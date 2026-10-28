import contextlib
import io
import json
import os
import sys
import threading
import time
import unittest
from http.server import HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vxlanlab import cli, kentik, sflow, snmp  # noqa: E402
from vxlanlab.engine import Engine  # noqa: E402
from vxlanlab.fabric import Fabric, ifindex  # noqa: E402
from test_kentik import FakeKentik  # noqa: E402

REM_SYS_NAME = snmp.LLDP_REM + (9,)
REM_PORT_ID = snmp.LLDP_REM + (7,)


class FiveStageFabricTests(unittest.TestCase):
    def setUp(self):
        self.f = Fabric("5stage")

    def test_shape_matches_the_demo(self):
        roles = {}
        for d in self.f.devices.values():
            roles.setdefault(d.role, []).append(d.name)
        self.assertEqual({r: len(n) for r, n in roles.items()},
                         {"edge": 2, "superspine": 2, "spine": 4, "leaf": 6, "tor": 8})
        self.assertIn("tor_h_dfw1", roles["tor"])
        self.assertEqual(len({d.mgmt_ip for d in self.f.devices.values()}), 22)
        # Its own IPs and names, so both labs can live in one Kentik account.
        three = Fabric()
        self.assertFalse({d.mgmt_ip for d in three.devices.values()} & {d.mgmt_ip for d in self.f.devices.values()})
        self.assertFalse(set(three.devices) & set(self.f.devices))

    def test_cross_pod_crosses_every_tier(self):
        db1, backup = self.f.hosts["db-01"], self.f.hosts["backup-01"]
        p = self.f.path(db1, backup, "tcp", 51103, 10566, 1450)
        self.assertEqual(p.kind, "l3vni")
        self.assertEqual([self.f.devices[n].role for n in p.via],
                         ["leaf", "spine", "superspine", "spine", "leaf"])
        self.assertEqual([h.device for h in p.hops][0], "tor_c_dfw1")
        self.assertEqual(p.hops[-1].device, "tor_g_dfw1")
        self.assertEqual(p.spine, p.via[1])
        prev = None
        for i, hop in enumerate(p.hops[1:], 1):
            s = sflow.decode_ethernet(hop.header)
            self.assertEqual((s["src_ip"], s["dst_ip"], s["vni"]), ("10.255.1.43", "10.255.1.47", 50010))
            self.assertEqual(s["inner"]["dst_ip"], "10.10.5.50")
            self.assertEqual(s["ttl"], 256 - i)
            dev = self.f.devices[hop.device]
            # Arrives on the port facing the previous switch.
            self.assertEqual(hop.in_if, ifindex(dev.port_to(prev or "tor_c_dfw1")))
            prev = hop.device
        self.assertEqual(p.hops[-1].out_if, ifindex(backup.port))

    def test_same_pod_turns_at_a_leaf(self):
        web1, web2 = self.f.hosts["web-01"], self.f.hosts["web-02"]
        p = self.f.path(web1, web2, "tcp", 51514, 8443, 300)
        self.assertEqual(p.kind, "l2vni")
        self.assertEqual(len(p.via), 1)
        self.assertEqual(self.f.devices[p.via[0]].role, "leaf")

    def test_ecmp_spreads_over_every_switch(self):
        e = Engine(self.f, ("127.0.0.1", 9), bind=False)
        for x in e.exporters.values():
            x.sock.close()
        used = {h.device for convs in [e.baseline] + list(e.injects.values()) for c in convs for h in c.path.hops}
        self.assertEqual(used, set(self.f.devices))

    def test_outside_clients_split_over_both_edges(self):
        clients = self.f.resolve("internet:10", "t")
        self.assertEqual({c.leaf for c in clients}, {"edge_a_dfw1", "edge_b_dfw1"})
        p = self.f.path(clients[0], self.f.hosts["web-03"], "tcp", 61544, 443, 1200)
        self.assertEqual([self.f.devices[n].role for n in p.via], ["superspine", "spine", "leaf"])
        self.assertEqual(p.vtep_src, self.f.devices[clients[0].leaf].vtep)

    def test_cross_vrf_dropped_at_tor(self):
        p = self.f.path(self.f.hosts["web-01"], self.f.hosts["etl-01"], "tcp", 51800, 9092, 60)
        self.assertEqual(p.kind, "dropped")
        self.assertEqual(p.hops[0].device, "tor_a_dfw1")

    def test_sflow_round_trip(self):
        e = Engine(self.f, ("127.0.0.1", 9), bind=False)
        for x in e.exporters.values():
            x.sock.close()
        e.set_inject("elephant", True)
        out = e.tick(1.0, dry_run=True)
        agents = set()
        for name, dgrams in out.items():
            for d in dgrams:
                dg = sflow.decode_datagram(d)
                self.assertEqual(dg["agent"], self.f.devices[name].mgmt_ip)
                if any(s["type"] == "flow" for s in dg["samples"]):
                    agents.add(name)
        self.assertGreater(len(agents), 15)

    def test_lldp_matches_links(self):
        links = set()
        for name in self.f.devices:
            agent = snmp.Agent(name, snmp.build_mib(self.f, name, time.time()), "public")

            def walk(prefix):
                out, oid = [], prefix
                while True:
                    req = snmp.encode_request(1, b"public", snmp.GET_NEXT, 1, [oid])
                    oid, val = snmp.decode_response(agent.handle(req))[2][0]
                    if val[0] == snmp.END_OF_MIB_VIEW or oid[:len(prefix)] != prefix:
                        return out
                    out.append((oid, val[1].decode()))

            sys_names = {oid[-2]: v for oid, v in walk(REM_SYS_NAME)}
            ports = {oid[-2]: v for oid, v in walk(REM_PORT_ID)}
            local = {oid[-1]: v for oid, v in walk(snmp.LLDP_LOC_PORT + (3,))}
            for idx, peer in sys_names.items():
                if peer in self.f.devices:
                    links.add((name, local[idx], peer, ports[idx]))
        # edge-superspine 4, superspine-spine 8, spine-leaf 12, leaf-tor 24: 48 cables, both ends.
        self.assertEqual(len(links), 96)
        for a, ap, b, bp in links:
            self.assertIn((b, bp, a, ap), links)
        self.assertIn(("tor_h_dfw1", "Ethernet51", "leaf_f_dfw1", "Ethernet4"), links)


class FiveStageOnboardTests(unittest.TestCase):
    def setUp(self):
        FakeKentik.calls = []
        FakeKentik.existing = {}
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
            rc = cli.main(["--topology", "5stage", "kentik", "onboard"] + list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_site_with_five_layers(self):
        rc, out, err = self.run_cli("--api-url", self.url, "--email", "a", "--token", "b", "--plan-id", "1",
                                    "--agent-id", "5", "--credential", "lab-snmp", "--site-name", "VXLAN Lab DFW1")
        self.assertEqual(rc, 0, err)
        self.assertIn("Verified: all 22", out)
        self.assertIn("192.168.251.x", out)
        created = [d for c in FakeKentik.calls if c[1].endswith("/batch_create") for d in c[3]["devices"]]
        self.assertEqual(len(created), 22)
        for d in created:
            self.assertEqual(d["nms"]["snmp"], {"credentialName": "lab-snmp", "port": 161})
            self.assertEqual(d["nms"]["ipAddress"], d["sendingIps"][0])
        names = {v: k for k, v in FakeKentik.existing.items()}
        layers = [(row["layers"][0]["name"], sorted(names[i] for i in row["layers"][0]["deviceIds"]))
                  for row in FakeKentik.sites[0]["architecture"]]
        self.assertEqual([l for l, _ in layers], ["Edge", "Superspine", "Spine", "Leaf", "Top of Rack"])
        self.assertEqual(layers[0][1], ["edge_a_dfw1", "edge_b_dfw1"])
        self.assertEqual(len(layers[4][1]), 8)

    def test_env_selects_topology(self):
        os.environ["VXLANLAB_TOPOLOGY"] = "5stage"
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                cli.main(["devices", "--csv"])
        finally:
            del os.environ["VXLANLAB_TOPOLOGY"]
        self.assertEqual(len(out.getvalue().strip().splitlines()), 23)

    def test_dry_run_lists_layers(self):
        rc, out, _ = self.run_cli("--dry-run", "--site-name", "VXLAN Lab DFW1")
        self.assertEqual(rc, 0)
        self.assertIn("Top of Rack = tor_a_dfw1", out)
        self.assertIn('"name": "Superspine"', out)
        self.assertEqual(kentik.layers_for(Fabric())[0][0], "Border")


if __name__ == "__main__":
    unittest.main()
