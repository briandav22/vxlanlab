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

    def _dim(self, dim_id):
        return next(d for d in self.dims.values() if str(d["id"]) == dim_id)

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
        d = self._dim(parts[1])
        p = dict(body["populator"], id=9000 + len(FakeV5.calls), dimension_id=d["id"])
        d["populators"].append(p)
        self._reply({"populator": p})

    def do_PUT(self):
        body = self._record()
        parts = self.path.strip("/").split("/")
        d = self._dim(parts[1])
        p = next(p for p in d["populators"] if str(p["id"]) == parts[3])
        p.update(body["populator"])
        self._reply({"populator": p})

    def do_DELETE(self):
        self._record()
        parts = self.path.strip("/").split("/")
        d = self._dim(parts[1])
        d["populators"] = [p for p in d["populators"] if str(p["id"]) != parts[3]]
        self._reply({})


class TestPlan(unittest.TestCase):
    def test_eight_dimensions_one_populator_per_subnet(self):
        rows = dimensions.load_subnets()
        want = dimensions.plan(rows)
        self.assertEqual([d["name"] for d in want],
                         ["c_src_segment", "c_dst_segment", "c_src_tenant", "c_dst_tenant",
                          "c_src_role", "c_dst_role", "c_src_zone", "c_dst_zone"])
        for d in want:
            self.assertLessEqual(len(d["name"]), 20)
            self.assertEqual(len(d["populators"]), len(rows))
            self.assertEqual({p["direction"] for p in d["populators"]}, {d["name"][2:5]})
        tenant = {p["addr"]: p["value"] for p in want[2]["populators"]}
        self.assertEqual(tenant["10.10.1.0/24"], "PAYMENTS")
        self.assertEqual(want[0]["populators"][0]["value"], "pay-web")
        self.assertEqual(want[3]["display_name"], "Dst Tenant")
        zones = {p["value"] for p in want[6]["populators"]}
        self.assertEqual(zones, {"overlay", "external", "underlay", "mgmt"})

    def test_diff(self):
        want = [{"value": "a", "direction": "src", "addr": "10.0.0.0/24"},
                {"value": "b", "direction": "src", "addr": "10.0.1.0/24"},
                {"value": "c", "direction": "src", "addr": "10.0.2.0/24"}]
        have = [{"id": 1, "value": "a", "direction": "src", "addr": "10.0.0.0/24"},
                {"id": 2, "value": "old", "direction": "src", "addr": "10.0.1.0/24"},
                {"id": 3, "value": "x", "direction": "src", "addr": "10.9.9.0/24"}]
        add, update, stale = dimensions.diff(want, have)
        self.assertEqual([p["addr"] for p in add], ["10.0.2.0/24"])
        self.assertEqual([(o["id"], p["value"]) for o, p in update], [(2, "b")])
        self.assertEqual([p["id"] for p in stale], [3])


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

    def test_creates_missing_and_fixes_existing(self):
        rc, out, err = self.run_cli("--email", "a", "--token", "b")
        self.assertEqual(rc, 0, err)
        n = len(dimensions.load_subnets())
        self.assertEqual(len(FakeV5.dims), 8)
        creates = [c for c in FakeV5.calls if c[0] == "POST" and c[1] == "/customdimension"]
        self.assertEqual(len(creates), 7)   # c_src_tenant already existed
        self.assertNotIn("populators", creates[0][2])
        tenant = FakeV5.dims["c_src_tenant"]
        self.assertEqual(tenant["id"], 42)
        by_addr = {p["addr"]: p["value"] for p in tenant["populators"]}
        self.assertEqual(by_addr["10.10.1.0/24"], "PAYMENTS")
        self.assertEqual(by_addr["172.16.0.0/16"], "GONE")   # not pruned by default
        self.assertIn("left alone", out)
        for d in FakeV5.dims.values():
            self.assertGreaterEqual(len(d["populators"]), n)

        FakeV5.calls = []
        rc, out, err = self.run_cli("--email", "a", "--token", "b")
        self.assertEqual(rc, 0, err)
        self.assertEqual([c[0] for c in FakeV5.calls], ["GET"])

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

    def test_prune(self):
        rc, _, err = self.run_cli("--email", "a", "--token", "b", "--prune")
        self.assertEqual(rc, 0, err)
        addrs = {p["addr"] for p in FakeV5.dims["c_src_tenant"]["populators"]}
        self.assertNotIn("172.16.0.0/16", addrs)

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
