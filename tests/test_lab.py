import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vxlanlab import sflow  # noqa: E402
from vxlanlab.engine import Engine  # noqa: E402
from vxlanlab.fabric import Fabric  # noqa: E402


class PathTests(unittest.TestCase):
    def setUp(self):
        self.f = Fabric()

    def test_l2_vni_keeps_host_macs(self):
        web1, web2 = self.f.hosts["web-01"], self.f.hosts["web-02"]
        p = self.f.path(web1, web2, "tcp", 51514, 8443, 300)
        self.assertEqual(p.kind, "l2vni")
        self.assertEqual(p.vni, 10010)
        pkt = sflow.decode_ethernet(p.hops[1].header)
        self.assertEqual(pkt["vni"], 10010)
        self.assertEqual(pkt["inner"]["src_mac"], web1.mac)
        self.assertEqual(pkt["inner"]["dst_mac"], web2.mac)

    def test_l3_vni_and_layers_per_hop(self):
        web1, db1 = self.f.hosts["web-01"], self.f.hosts["db-01"]
        p = self.f.path(web1, db1, "tcp", 51022, 5432, 900)
        self.assertEqual(p.kind, "l3vni")
        self.assertEqual(p.vni, 50010)
        access, spine, dst_leaf = p.hops
        # Access port: inner packet only.
        a = sflow.decode_ethernet(access.header)
        self.assertNotIn("vni", a)
        self.assertEqual((a["src_ip"], a["dst_ip"]), ("10.10.1.21", "10.10.2.45"))
        # Spine and destination uplink: VTEP outside, servers inside.
        for hop in (spine, dst_leaf):
            s = sflow.decode_ethernet(hop.header)
            self.assertEqual((s["src_ip"], s["dst_ip"], s["dport"]), ("10.255.0.11", "10.255.0.13", 4789))
            self.assertEqual((s["inner"]["src_ip"], s["inner"]["dst_ip"], s["inner"]["dport"]),
                             ("10.10.1.21", "10.10.2.45", 5432))
            self.assertEqual(hop.frame_len, 900 + 14 + 50 + 4)
        self.assertEqual(spine.device, p.spine)

    def test_cross_vrf_is_dropped_unless_leaked(self):
        web1, etl2 = self.f.hosts["web-01"], self.f.hosts["etl-02"]
        p = self.f.path(web1, etl2, "tcp", 51800, 9092, 60)
        self.assertEqual(p.kind, "dropped")
        self.assertEqual(len(p.hops), 1)
        self.assertEqual(p.hops[0].out_if, 0)
        leaked = self.f.path(web1, etl2, "tcp", 51800, 9092, 60, leak=True)
        self.assertEqual(leaked.vni, 50020)

    def test_internet_client_enters_at_border_leaf(self):
        client = self.f.resolve("internet:1", "t")[0]
        p = self.f.path(client, self.f.hosts["web-01"], "tcp", 61544, 443, 1200)
        self.assertEqual(p.hops[0].device, "border-leaf")
        self.assertEqual(p.vtep_src, "10.255.0.14")


class SflowTests(unittest.TestCase):
    def test_round_trip(self):
        f = Fabric()
        e = Engine(f, ("127.0.0.1", 9), bind=False)
        e.set_inject("elephant", True)
        out = e.tick(1.0, dry_run=True)
        n = 0
        for name, dgrams in out.items():
            agent = f.devices[name].mgmt_ip
            for d in dgrams:
                self.assertLessEqual(len(d), 1400)
                dg = sflow.decode_datagram(d)
                self.assertEqual(dg["agent"], agent)
                for s in dg["samples"]:
                    if s["type"] == "flow":
                        n += 1
                        self.assertEqual(s["rate"], 1024)
                        self.assertIn("src_ip", s["packet"])
        self.assertGreater(n, 100)


if __name__ == "__main__":
    unittest.main()
