import os
import socket
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vxlanlab import snmp  # noqa: E402
from vxlanlab.engine import Engine  # noqa: E402
from vxlanlab.fabric import Fabric, ifindex  # noqa: E402

SYS_NAME = (1, 3, 6, 1, 2, 1, 1, 5, 0)
IF_NAME = snmp.IFX_ENTRY + (1,)
REM_SYS_NAME = snmp.LLDP_REM + (9,)
REM_PORT_ID = snmp.LLDP_REM + (7,)


class SnmpTests(unittest.TestCase):
    def setUp(self):
        self.f = Fabric()
        self.agents = {name: snmp.Agent(name, snmp.build_mib(self.f, name, time.time()), "public")
                       for name in self.f.devices}

    def ask(self, name, pdu, oids, version=1, community=b"public", nr=0, reps=0):
        req = snmp.encode_request(version, community, pdu, 42, oids, nr, reps)
        resp = self.agents[name].handle(req)
        return None if resp is None else snmp.decode_response(resp)

    def walk(self, name, prefix):
        out, oid = [], prefix
        while True:
            _, _, vbs = self.ask(name, snmp.GET_NEXT, [oid])
            oid, val = vbs[0]
            if val[0] == snmp.END_OF_MIB_VIEW or oid[:len(prefix)] != prefix:
                return out
            out.append((oid, val[1]))

    def test_ber_roundtrip(self):
        for oid in [(1, 3, 6, 1, 2, 1, 1, 5, 0), (1, 0, 8802, 1, 1, 2, 1, 4, 1, 1, 9, 0, 49, 4),
                    (2, 999, 128, 16383, 16384)]:
            self.assertEqual(snmp.decode_oid(snmp._oid_body(oid)), oid)
        for v in (0, 127, 128, 255, 256, -1, -129, 2 ** 31 - 1):
            self.assertEqual(snmp.decode_int(snmp._int_body(v)), v)

    def test_sysname_is_device_name(self):
        for name in self.f.devices:
            _, _, vbs = self.ask(name, snmp.GET, [SYS_NAME])
            self.assertEqual(vbs[0][1], (snmp.OCTET_STRING, name.encode()))

    def test_if_names_match_sflow_ifindexes(self):
        names = dict((oid[-1], v.decode()) for oid, v in self.walk("leaf-01", IF_NAME))
        self.assertEqual(names[49], "Ethernet49")
        self.assertEqual(names[50], "Ethernet50")
        self.assertEqual(names[1], "Ethernet1")   # web-01
        for idx, name in names.items():
            if idx != snmp.MGMT_IFINDEX:
                self.assertEqual(ifindex(name), idx)

    def test_lldp_links_are_symmetric(self):
        """Every switch-to-switch neighbor is reported from both ends, with matching ports."""
        links = set()
        for name in self.f.devices:
            sys_names = {oid[-2]: v.decode() for oid, v in self.walk(name, REM_SYS_NAME)}
            ports = {oid[-2]: v.decode() for oid, v in self.walk(name, REM_PORT_ID)}
            local = {oid[-1]: v.decode() for oid, v in self.walk(name, snmp.LLDP_LOC_PORT + (3,))}
            for idx, peer in sys_names.items():
                if peer in self.f.devices:
                    links.add((name, local[idx], peer, ports[idx]))
        self.assertEqual(len(links), 16)   # 8 cables, seen from both sides
        for a, ap, b, bp in links:
            self.assertIn((b, bp, a, ap), links)
        self.assertIn(("spine-02", "Ethernet3", "leaf-02", "Ethernet50"), links)

    def test_servers_and_firewall_are_neighbors(self):
        leaf3 = {oid[-2]: v.decode() for oid, v in self.walk("leaf-03", REM_SYS_NAME)}
        self.assertEqual(leaf3[1], "db-01")
        border = {oid[-2]: v.decode() for oid, v in self.walk("border-leaf", REM_SYS_NAME)}
        self.assertEqual(border[1], "fw-edge")

    def test_getbulk_and_missing(self):
        _, _, vbs = self.ask("spine-01", snmp.GET_BULK, [IF_NAME], nr=0, reps=3)
        self.assertEqual([v[1].decode() for _, v in vbs], ["Ethernet1", "Ethernet2", "Ethernet3"])
        _, _, vbs = self.ask("spine-01", snmp.GET, [(1, 3, 6, 1, 2, 1, 1, 9, 0)])
        self.assertEqual(vbs[0][1][0], snmp.NO_SUCH_OBJECT)
        es, ei, _ = self.ask("spine-01", snmp.GET, [(1, 3, 6, 1, 2, 1, 1, 9, 0)], version=0)
        self.assertEqual((es, ei), (snmp.NO_SUCH_NAME, 1))
        _, _, vbs = self.ask("spine-01", snmp.GET_NEXT, [(2, 0)])
        self.assertEqual(vbs[0][1][0], snmp.END_OF_MIB_VIEW)

    def test_wrong_community_is_ignored(self):
        self.assertIsNone(self.ask("spine-01", snmp.GET, [SYS_NAME], community=b"nope"))

    def test_counters_follow_engine(self):
        e = Engine(self.f, ("127.0.0.1", 9), bind=False)
        for x in e.exporters.values():
            x.sock.close()
        agent = snmp.Agent("spine-01", snmp.build_mib(self.f, "spine-01", time.time(), e.counters),
                           "public")
        hc_in = snmp.IFX_ENTRY + (6, 2)   # spine-01 Ethernet2 faces leaf-01
        get = lambda: snmp.decode_response(agent.handle(
            snmp.encode_request(1, b"public", snmp.GET, 1, [hc_in])))[2][0][1][1]
        before = get()
        e.tick(1.0, dry_run=True)
        self.assertGreater(get(), before)

    def test_udp_server(self):
        try:
            server = snmp.SnmpServer(self.f, port=0, listen_ip="127.0.0.1").start()
        except OSError as err:
            self.skipTest("can't bind: %s" % err)
        try:
            port = server.sockets[0].getsockname()[1]
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(2)
            s.sendto(snmp.encode_request(1, b"public", snmp.GET, 7, [SYS_NAME]), ("127.0.0.1", port))
            _, _, vbs = snmp.decode_response(s.recv(65535))
            self.assertEqual(vbs[0][1][1], b"spine-01")
            s.close()
        finally:
            server.close()


if __name__ == "__main__":
    unittest.main()
