import contextlib
import io
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vxlanlab import cli, kentik  # noqa: E402
from vxlanlab.fabric import Fabric  # noqa: E402


class FakeKentik(BaseHTTPRequestHandler):
    """Answers the three Device API calls the onboard command makes."""
    existing = {}
    calls = []

    def log_message(self, *a):
        pass

    def _reply(self, body):
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _record(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n)) if n else None
        FakeKentik.calls.append((self.command, self.path, self.headers, body))
        return body

    def do_GET(self):
        self._record()
        self._reply({"devices": [{"id": i, "deviceName": n} for n, i in self.existing.items()]})

    def do_POST(self):
        body = self._record()
        self._reply({"devices": [dict(d, id=str(100 + k)) for k, d in enumerate(body["devices"])]})

    def do_PUT(self):
        self._reply(self._record())


class PayloadTests(unittest.TestCase):
    def setUp(self):
        self.f = Fabric()

    def test_one_router_per_switch(self):
        devs = kentik.build_devices(self.f, plan_id=7, site_id=3, agent_id="9", credential="lab",
                                    template_id=42)
        self.assertEqual([d["deviceName"] for d in devs], list(self.f.devices))
        leaf = devs[[d["deviceName"] for d in devs].index("leaf-01")]
        self.assertEqual(leaf["sendingIps"], ["192.168.250.11"])
        self.assertEqual(leaf["nms"], {"agentId": "9", "ipAddress": "192.168.250.11",
                                       "snmp": {"credentialName": "lab", "port": 161}})
        self.assertEqual((leaf["planId"], leaf["siteId"], leaf["monitoringTemplateId"]), (7, 3, 42))
        self.assertEqual(leaf["deviceSampleRate"], 1024)
        self.assertNotIn("id", leaf)

    def test_optional_parts_left_out(self):
        dev = kentik.build_devices(self.f, plan_id=None, only=["spine-01"])[0]
        for key in ("planId", "siteId", "nms", "monitoringTemplateId"):
            self.assertNotIn(key, dev)
        ping_only = kentik.build_devices(self.f, plan_id=1, agent_id="9", only=["spine-01"])[0]
        self.assertNotIn("snmp", ping_only["nms"])

    def test_underscores(self):
        names = [d["deviceName"] for d in kentik.build_devices(self.f, 1, underscores=True)]
        self.assertIn("border_leaf", names)
        self.assertFalse(any("-" in n for n in names))

    def test_split_existing(self):
        devs = kentik.build_devices(self.f, 1, only=["spine-01", "leaf-01"])
        create, update = kentik.split_existing(devs, {"leaf-01": "555"})
        self.assertEqual([d["deviceName"] for d in create], ["spine-01"])
        self.assertEqual(update[0]["id"], "555")


class OnboardCommandTests(unittest.TestCase):
    def setUp(self):
        FakeKentik.calls = []
        FakeKentik.existing = {"leaf-02": "777"}
        self.srv = HTTPServer(("127.0.0.1", 0), FakeKentik)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d" % self.srv.server_port

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(["kentik", "onboard"] + list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_creates_new_and_updates_existing(self):
        rc, out, _ = self.run_cli("--api-url", self.url, "--email", "a@b.c", "--token", "t0k",
                                  "--plan-id", "11", "--agent-id", "5", "--credential", "lab")
        self.assertEqual(rc, 0, out)
        methods = [(c[0], c[1].split("?")[0]) for c in FakeKentik.calls]
        self.assertEqual(methods, [("GET", kentik.DEVICE_PATH),
                                   ("POST", kentik.DEVICE_PATH + "/batch_create"),
                                   ("PUT", kentik.DEVICE_PATH + "/batch_update")])
        headers = FakeKentik.calls[0][2]
        self.assertEqual((headers["X-CH-Auth-Email"], headers["X-CH-Auth-API-Token"]), ("a@b.c", "t0k"))
        created = [d["deviceName"] for d in FakeKentik.calls[1][3]["devices"]]
        self.assertEqual(len(created), 5)
        self.assertNotIn("leaf-02", created)
        self.assertEqual(FakeKentik.calls[2][3]["devices"], [dict(
            kentik.build_devices(Fabric(), 11, agent_id="5", credential="lab", only=["leaf-02"])[0],
            id="777")])
        self.assertNotIn("t0k", out)

    def test_dry_run_makes_no_calls(self):
        rc, out, _ = self.run_cli("--api-url", self.url, "--dry-run")
        self.assertEqual(rc, 0)
        payload = json.loads(out[out.index("{"):])
        self.assertEqual(len(payload["devices"]), 6)
        self.assertEqual(FakeKentik.calls, [])

    def test_needs_credentials(self):
        env = {k: os.environ.pop(k) for k in ("KENTIK_EMAIL", "KENTIK_API_TOKEN") if k in os.environ}
        try:
            rc, _, err = self.run_cli("--api-url", self.url, "--plan-id", "1")
        finally:
            os.environ.update(env)
        self.assertEqual(rc, 2)
        self.assertIn("KENTIK_API_TOKEN", err)
        self.assertEqual(FakeKentik.calls, [])

    def test_api_error_is_reported(self):
        rc, _, err = self.run_cli("--api-url", "http://127.0.0.1:1", "--email", "a", "--token", "b",
                                  "--plan-id", "1")
        self.assertEqual(rc, 1)
        self.assertIn("Kentik API error", err)


if __name__ == "__main__":
    unittest.main()
