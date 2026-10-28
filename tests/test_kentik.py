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
    """A stateful stand-in for the Device API calls the onboard command makes.

    reject: names that fail. batch_create lists them under failedDevices with a
    200, like the real API; a single create answers 400 with a reason.
    silent: batch_create answers 200 {} and creates nothing.
    """
    existing = {}
    calls = []
    reject = set()
    silent = False
    sites = []

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
        FakeKentik.calls.append((self.command, self.path, self.headers, body))
        return body

    def _add(self, dev):
        dev = dict(dev, id=str(100 + len(self.existing)))
        FakeKentik.existing[dev["deviceName"]] = dev["id"]
        return dev

    def do_GET(self):
        self._record()
        if self.path.startswith(kentik.SITE_PATH):
            return self._reply({"sites": self.sites})
        self._reply({"devices": [{"id": i, "deviceName": n} for n, i in self.existing.items()]})

    def do_POST(self):
        body = self._record()
        if self.path == kentik.SITE_PATH:
            site = dict(body["site"], id=str(900 + len(self.sites)))
            FakeKentik.sites.append(site)
            return self._reply({"site": site})
        if self.path.endswith("/batch_create"):
            if self.silent:
                return self._reply({})
            ok = [self._add(d) for d in body["devices"] if d["deviceName"] not in self.reject]
            bad = [d["deviceName"] for d in body["devices"] if d["deviceName"] in self.reject]
            return self._reply({"devices": ok, "failedDevices": bad})
        dev = body["device"]
        if dev["deviceName"] in self.reject:
            return self._reply({"code": 3, "message": "invalid device_name"}, 400)
        self._reply({"device": self._add(dev)})

    def do_PUT(self):
        body = self._record()
        if self.path.startswith(kentik.SITE_PATH + "/"):
            FakeKentik.sites = [body["site"] if s["id"] == body["site"]["id"] else s for s in self.sites]
            return self._reply(body)
        self._reply(body)


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
        FakeKentik.reject = set()
        FakeKentik.silent = False
        FakeKentik.sites = []
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
                                   ("PUT", kentik.DEVICE_PATH + "/batch_update"),
                                   ("GET", kentik.DEVICE_PATH)])
        self.assertIn("Verified: all 6", out)
        headers = FakeKentik.calls[0][2]
        self.assertEqual((headers["X-CH-Auth-Email"], headers["X-CH-Auth-API-Token"]), ("a@b.c", "t0k"))
        created = [d["deviceName"] for d in FakeKentik.calls[1][3]["devices"]]
        self.assertEqual(len(created), 5)
        self.assertNotIn("leaf-02", created)
        self.assertEqual(FakeKentik.calls[2][3]["devices"], [dict(
            kentik.build_devices(Fabric(), 11, agent_id="5", credential="lab", only=["leaf-02"])[0],
            id="777")])
        self.assertNotIn("t0k", out)

    def creds(self):
        return ["--api-url", self.url, "--email", "a", "--token", "b", "--plan-id", "1"]

    def test_failed_devices_are_reported_with_reason(self):
        FakeKentik.reject = {"spine-01"}
        rc, out, err = self.run_cli(*self.creds())
        self.assertEqual(rc, 1)
        self.assertIn("1 of 6 device(s) are NOT in Kentik", err)
        self.assertIn("spine-01", err)
        self.assertIn("invalid device_name", err)
        self.assertIn("--underscores", err)
        self.assertNotIn("Verified", out)

    def test_silent_batch_is_caught_and_retried(self):
        FakeKentik.silent = True
        rc, out, err = self.run_cli(*self.creds())
        self.assertEqual(rc, 0, err)
        self.assertEqual(out.count("(on retry)"), 5)
        self.assertIn("Verified: all 6", out)

    def test_silent_batch_with_failing_retries(self):
        FakeKentik.silent = True
        FakeKentik.reject = set(Fabric().devices) - {"leaf-02"}
        rc, _, err = self.run_cli(*self.creds())
        self.assertEqual(rc, 1)
        self.assertIn("5 of 6", err)

    def test_credential_name_env_alias(self):
        env = {k: os.environ.pop(k) for k in ("KENTIK_SNMP_CREDENTIAL",) if k in os.environ}
        os.environ["KENTIK_CREDENTIAL_NAME"] = "lab-snmp"
        try:
            rc, out, err = self.run_cli("--dry-run", "--agent-id", "6017", "--device", "leaf-03")
        finally:
            del os.environ["KENTIK_CREDENTIAL_NAME"]
            os.environ.update(env)
        self.assertEqual(rc, 0)
        dev = json.loads(out[out.index("{"):])["devices"][0]
        self.assertEqual(dev["nms"]["snmp"], {"credentialName": "lab-snmp", "port": 161})
        self.assertNotIn("WARNING", err)

    def test_no_credential_warns(self):
        env = {k: os.environ.pop(k) for k in ("KENTIK_SNMP_CREDENTIAL", "KENTIK_CREDENTIAL_NAME")
               if k in os.environ}
        try:
            _, out, err = self.run_cli("--dry-run", "--agent-id", "6017")
        finally:
            os.environ.update(env)
        self.assertIn("ICMP only", err)
        self.assertNotIn('"snmp"', out)

    def layers(self, site):
        names = {v: k for k, v in FakeKentik.existing.items()}
        return [[(l["name"], sorted(names[i] for i in l["deviceIds"])) for l in row["layers"]]
                for row in site["architecture"]]

    def test_site_created_and_layers_set(self):
        rc, out, err = self.run_cli(*self.creds(), "--site-name", "VXLAN Lab")
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(FakeKentik.sites), 1)
        site = FakeKentik.sites[0]
        self.assertEqual((site["title"], site["type"]), ("VXLAN Lab", "SITE_TYPE_DATA_CENTER"))
        creates = [c[3] for c in FakeKentik.calls if c[1].endswith("/batch_create")]
        self.assertEqual({d["siteId"] for d in creates[0]["devices"]}, {900})
        updates = [c[3] for c in FakeKentik.calls if c[1].endswith("/batch_update")]
        self.assertEqual(updates[0]["devices"][0]["siteId"], 900)
        self.assertEqual(self.layers(site), [[("Border", ["border-leaf"])],
                                             [("Spine", ["spine-01", "spine-02"])],
                                             [("Leaf", ["leaf-01", "leaf-02", "leaf-03"])]])

    def test_existing_site_reused_and_other_layers_kept(self):
        FakeKentik.existing["core-01"] = "555"
        FakeKentik.sites = [{"id": "42", "title": "VXLAN Lab", "type": "SITE_TYPE_DATA_CENTER",
                             "architecture": [{"layers": [{"name": "Core", "deviceIds": ["555"]}]},
                                              {"layers": [{"name": "Leaf", "deviceIds": ["777", "555x"]}]}]}]
        rc, out, err = self.run_cli(*self.creds(), "--site-name", "VXLAN Lab")
        self.assertEqual(rc, 0, err)
        site = FakeKentik.sites[0]
        self.assertEqual(len(FakeKentik.sites), 1)
        rows = [[l["name"] for l in row["layers"]] for row in site["architecture"]]
        self.assertEqual(rows, [["Core"], ["Leaf"], ["Border"], ["Spine"]])
        leaf = site["architecture"][1]["layers"][0]["deviceIds"]
        self.assertIn("555x", leaf)     # someone else's device stays
        self.assertEqual(leaf.count("777"), 1)

    def test_no_layers(self):
        rc, _, err = self.run_cli(*self.creds(), "--site-name", "VXLAN Lab", "--no-layers")
        self.assertEqual(rc, 0, err)
        self.assertFalse(any(c[0] == "PUT" and c[1].startswith(kentik.SITE_PATH) for c in FakeKentik.calls))

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
