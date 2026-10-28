import contextlib
import io
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vxlanlab import cli, dimensions  # noqa: E402


class FakeV5(BaseHTTPRequestHandler):
    """A stateful stand-in for the v5 custom dimension and populator calls."""
    dims = {}
    calls = []
    throttle = 0   # answer this many more POSTs with 429

    def log_message(self, *a):
        pass

    def _reply(self, body, code=200):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _record(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n)) if n else None
        FakeV5.calls.append((self.command, self.path, body))
        return body

    def do_GET(self):
        self._record()
        self._reply({"customDimensions": list(self.dims.values())})

    def do_POST(self):
        body = self._record()
        if FakeV5.throttle:
            FakeV5.throttle -= 1
            self.send_response(429)
            self.send_header("Retry-After", "0")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        parts = self.path.strip("/").split("/")
        if parts == ["customdimension"]:
            d = dict(body, id=500 + len(self.dims), populators=[])
            FakeV5.dims[d["name"]] = d
            return self._reply({"customDimension": {k: v for k, v in d.items() if k != "populators"}})
        # /batch/customdimensions/<name>/populators
        d = FakeV5.dims[parts[2]]
        assert body["replace_all"] and body["complete"]
        d["populators"] = [{"value": u["value"], "direction": c["direction"], "addr": ",".join(c["addr"])}
                           for u in body["upserts"] for c in u["criteria"]]
        self._reply({"message": "Accepted batch", "guid": "abc"})


class TestPlan(unittest.TestCase):
    def test_one_populator_per_value(self):
        rows = dimensions.load_subnets()
        want = dimensions.plan(rows)
        self.assertEqual([d["name"] for d in want],
                         ["c_src_segment", "c_dst_segment", "c_src_tenant", "c_dst_tenant",
                          "c_src_role", "c_dst_role", "c_src_zone", "c_dst_zone"])
        for d in want:
            self.assertLessEqual(len(d["name"]), 20)
            values = [u["value"] for u in d["upserts"]]
            self.assertEqual(len(values), len(set(values)))
            self.assertEqual(sum(len(u["criteria"][0]["addr"]) for u in d["upserts"]), len(rows))
            self.assertEqual({u["criteria"][0]["direction"] for u in d["upserts"]}, {d["name"][2:5]})
        tenant = {u["value"]: u["criteria"][0]["addr"] for u in want[2]["upserts"]}
        self.assertEqual(sorted(tenant), ["ANALYTICS", "PAYMENTS", "UNDERLAY"])
        self.assertEqual(len(tenant["UNDERLAY"]), 6)
        self.assertIn("10.255.1.0/24", tenant["UNDERLAY"])
        self.assertEqual(want[0]["upserts"][0]["value"], "pay-web")
        self.assertEqual(want[3]["display_name"], "Dst Tenant")
        self.assertEqual({u["value"] for u in want[6]["upserts"]}, {"overlay", "external", "underlay", "mgmt"})


class TestThrottle(unittest.TestCase):
    def test_retry_after(self):
        self.assertEqual(dimensions.retry_after("5"), 5.0)
        self.assertIsNone(dimensions.retry_after(None))
        self.assertIsNone(dimensions.retry_after("Wed, 21 Oct 2026 07:28:00 GMT"))

    def test_calls_are_spaced_out(self):
        waits = []
        c = dimensions.Client("a", "b", "http://127.0.0.1:1", delay=1.0, retries=0, sleep=waits.append)
        for _ in range(3):
            with self.assertRaises(Exception):
                c._call("GET", "/customdimensions")
        self.assertEqual(len(waits), 2)
        self.assertTrue(all(0 < w <= 1.0 for w in waits))


class TestCommand(unittest.TestCase):
    def setUp(self):
        FakeV5.calls = []
        FakeV5.throttle = 0
        FakeV5.dims = {"c_src_tenant": {"id": 42, "name": "c_src_tenant", "display_name": "Src Tenant",
                                        "type": "string", "populators": [
                                            {"id": 1, "value": "WRONG", "direction": "src",
                                             "addr": "10.10.1.0/24"},
                                            {"id": 2, "value": "GONE", "direction": "src",
                                             "addr": "172.16.0.0/16"}]}}
        self.srv = HTTPServer(("127.0.0.1", 0), FakeV5)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d" % self.srv.server_port

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(["kentik", "dimensions", "--api-url", self.url, "--delay", "0"] + list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_creates_missing_and_replaces_populators(self):
        rc, out, err = self.run_cli("--email", "a", "--token", "b")
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(FakeV5.dims), 8)
        creates = [c for c in FakeV5.calls if c[0] == "POST" and c[1] == "/customdimension"]
        self.assertEqual(len(creates), 7)   # c_src_tenant already existed
        self.assertNotIn("upserts", creates[0][2])
        batches = [c for c in FakeV5.calls if c[1].startswith("/batch/")]
        self.assertEqual(len(batches), 8)
        tenant = FakeV5.dims["c_src_tenant"]
        self.assertEqual(tenant["id"], 42)
        by_value = {p["value"]: p["addr"] for p in tenant["populators"]}
        self.assertEqual(sorted(by_value), ["ANALYTICS", "PAYMENTS", "UNDERLAY"])   # WRONG and GONE replaced
        self.assertEqual(len(by_value["UNDERLAY"].split(",")), 6)

        FakeV5.calls = []
        rc, _, err = self.run_cli("--email", "a", "--token", "b")
        self.assertEqual(rc, 0, err)
        self.assertEqual([c[0] for c in FakeV5.calls], ["GET"] + ["POST"] * 8)
        self.assertTrue(all(c[1].startswith("/batch/") for c in FakeV5.calls[1:]))

    def test_rate_limit_is_retried(self):
        FakeV5.throttle = 3
        rc, out, err = self.run_cli("--email", "a", "--token", "b")
        self.assertEqual(rc, 0, err)
        self.assertEqual(out.count("rate limited"), 3)
        self.assertEqual(len(FakeV5.dims), 8)

    def test_rate_limit_gives_up(self):
        FakeV5.throttle = 1000
        rc, _, err = self.run_cli("--email", "a", "--token", "b", "--retries", "1")
        self.assertEqual(rc, 1)
        self.assertIn("HTTP 429", err)

    def test_dry_run_and_credentials(self):
        rc, out, _ = self.run_cli("--dry-run")
        self.assertEqual(rc, 0)
        self.assertEqual(len(json.loads(out[out.index("["):])), 8)
        env = {k: os.environ.pop(k) for k in ("KENTIK_EMAIL", "KENTIK_API_TOKEN") if k in os.environ}
        try:
            rc, _, err = self.run_cli()
        finally:
            os.environ.update(env)
        self.assertEqual(rc, 2)
        self.assertIn("KENTIK_API_TOKEN", err)
        self.assertEqual(FakeV5.calls, [])


if __name__ == "__main__":
    unittest.main()
