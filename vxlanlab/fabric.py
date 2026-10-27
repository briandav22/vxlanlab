"""Fabric model: devices, hosts, segments, and how a packet crosses the fabric.

For each conversation direction we work out, once, every place an sFlow
sample can be taken (device, input and output interface) and the exact packet
header a sample there would contain. Ingress sampling is modeled, which is the
default on most data center switches.

  src leaf, host-facing port   inner packet only (no VXLAN yet)
  spine, port facing src leaf  outer VTEP headers + VXLAN + inner packet
  dst leaf, uplink port        outer VTEP headers + VXLAN + inner packet
"""
from __future__ import annotations

import ipaddress
import json
import os
import random
from typing import Dict, List, Optional

from . import packets as pk

DEFAULT_TOPOLOGY = os.path.join(os.path.dirname(__file__), "topology.json")


def ifindex(name: str) -> int:
    """Ethernet49 -> 49. Matches how most switches number front-panel ports."""
    digits = "".join(c for c in name if c.isdigit())
    return int(digits) if digits else 0


def fake_mac(seed: str, oui: str = "02:1c:73") -> str:
    r = random.Random(seed)
    return oui + ":" + ":".join("%02x" % r.randrange(256) for _ in range(3))


class Device:
    def __init__(self, d: Dict):
        self.name = d["name"]
        self.role = d["role"]
        self.mgmt_ip = d["mgmt_ip"]
        self.vtep = d.get("vtep")
        self.loopback = d.get("loopback")
        self.uplinks = d.get("uplinks", {})        # spine name -> local port
        self.downlinks = d.get("downlinks", {})    # leaf name -> local port
        self.edge_ports = d.get("edge_ports", {})  # vrf -> local port
        self.mac = fake_mac(self.name)              # router / system MAC


class Endpoint:
    """Something that sends or receives traffic: a server, or an outside client."""

    def __init__(self, name: str, ip: str, mac: str, vrf: str, leaf: str, port: str,
                 vlan: int = 0, vni: int = 0, external: bool = False):
        self.name, self.ip, self.mac, self.vrf = name, ip, mac, vrf
        self.leaf, self.port, self.vlan, self.vni = leaf, port, vlan, vni
        self.external = external


class Hop:
    """One place a sample of this conversation can be taken."""

    __slots__ = ("device", "in_if", "out_if", "header", "frame_len", "src_vlan", "dst_vlan")

    def __init__(self, device: str, in_if: int, out_if: int, header: bytes, frame_len: int,
                 src_vlan: int = 0, dst_vlan: int = 0):
        self.device, self.in_if, self.out_if = device, in_if, out_if
        self.header, self.frame_len = header, frame_len
        self.src_vlan, self.dst_vlan = src_vlan, dst_vlan


class Path:
    """How one direction of one conversation crosses the fabric."""

    def __init__(self, kind: str, hops: List[Hop], vni: int = 0, spine: Optional[str] = None,
                 vtep_src: Optional[str] = None, vtep_dst: Optional[str] = None,
                 outer_sport: int = 0):
        self.kind, self.hops, self.vni, self.spine = kind, hops, vni, spine
        self.vtep_src, self.vtep_dst, self.outer_sport = vtep_src, vtep_dst, outer_sport


class Fabric:
    def __init__(self, path: str = DEFAULT_TOPOLOGY, header_bytes: Optional[int] = None):
        with open(path) as f:
            self.cfg = json.load(f)
        c = self.cfg
        self.header_bytes = header_bytes or c.get("header_bytes", 128)
        self.gw_mac = c.get("anycast_gateway_mac", "00:00:5e:00:01:01")
        self.fw_mac = c.get("firewall_mac", "00:1b:17:00:0f:01")
        self.vrfs = c["vrfs"]
        self.devices: Dict[str, Device] = {d["name"]: Device(d) for d in c["devices"]}
        self.spines = [d for d in self.devices.values() if d.role == "spine"]
        self.border = next((d for d in self.devices.values() if d.role == "border"), None)
        self.segments = {s["vlan"]: s for s in c["segments"]}
        self.hosts: Dict[str, Endpoint] = {}
        for h in c["hosts"]:
            seg = self.segments[h["vlan"]]
            self.hosts[h["name"]] = Endpoint(h["name"], h["ip"], fake_mac(h["name"], "52:54:00"),
                                             seg["vrf"], h["leaf"], h["port"], h["vlan"], seg["vni"])
        self.external = c.get("external", {})

    # ------------------------------------------------------------ endpoints

    def external_clients(self, kind: str, count: int, seed: str) -> List[Endpoint]:
        ext = self.external[kind]
        addrs: List[str] = []
        for pool in ext["pools"]:
            addrs.extend(str(a) for a in ipaddress.ip_network(pool).hosts())
        r = random.Random(seed)
        if count <= len(addrs):
            picked = r.sample(addrs, count)
        else:
            picked = [r.choice(addrs) for _ in range(count)]
        vrf = ext["vrf"]
        port = self.border.edge_ports[vrf]
        return [Endpoint("%s-%s" % (kind, ip), ip, self.fw_mac, vrf, self.border.name, port,
                         external=True) for ip in picked]

    def resolve(self, ref: str, seed: str) -> List[Endpoint]:
        """'web-01' -> [host]; 'internet:300' -> 300 outside clients."""
        if ":" in ref:
            kind, n = ref.split(":", 1)
            return self.external_clients(kind, int(n), seed)
        if ref in self.external:
            return self.external_clients(ref, 1, seed)
        return [self.hosts[ref]]

    # ------------------------------------------------------------ paths

    def _uplink_port(self, leaf: str, spine: str) -> str:
        return self.devices[leaf].uplinks[spine]

    def _downlink_port(self, spine: str, leaf: str) -> str:
        return self.devices[spine].downlinks[leaf]

    def path(self, src: Endpoint, dst: Endpoint, proto: str, sport: int, dport: int,
             ip_len: int, leak: bool = False) -> Path:
        hb = self.header_bytes
        same_segment = (not src.external and not dst.external and src.vlan == dst.vlan)
        routed = not same_segment
        inner_frame_len = ip_len + pk.ETH_HDR
        src_dev = self.devices[src.leaf]
        src_in = ifindex(src.port)
        src_vlan = src.vlan

        # What the packet looks like arriving on the source leaf's access port.
        first_dst_mac = dst.mac if same_segment else self.gw_mac
        first_src_mac = src.mac
        access_hdr = pk.inner_packet(first_src_mac, first_dst_mac, src.ip, dst.ip, proto,
                                     sport, dport, ip_len, ttl=64)

        def access_hop(out_if: int, dst_vlan: int = 0) -> Hop:
            return Hop(src_dev.name, src_in, out_if,
                       pk.pad_header(access_hdr, inner_frame_len, hb),
                       inner_frame_len + 4, src_vlan, dst_vlan)

        # Different VRF and nobody leaked routes: dropped at the first leaf.
        if src.vrf != dst.vrf and not leak:
            return Path("dropped", [access_hop(0)])

        # Both ends on the same leaf: switched or routed locally, never encapsulated.
        if src.leaf == dst.leaf:
            return Path("local", [access_hop(ifindex(dst.port), dst.vlan)])

        # Crosses the fabric: pick a spine by hashing the inner 5-tuple, like ECMP does.
        h = pk.ecmp_hash(src.ip, dst.ip, proto, sport, dport)
        spine = self.spines[(h >> 16) % len(self.spines)]
        outer_sport = pk.vxlan_source_port(h)
        # Routed traffic carries the L3 VNI of the VRF the destination lives in. Without
        # leaking that's the same VRF as the source; with leaking it's the other one.
        vni = src.vni if same_segment else self.vrfs[dst.vrf]["l3vni"]
        dst_dev = self.devices[dst.leaf]

        if routed:
            inner_src_mac, inner_dst_mac = src_dev.mac, dst_dev.mac
        else:
            inner_src_mac, inner_dst_mac = src.mac, dst.mac
        inner_ttl = 63 if routed else 64
        inner_hdr = pk.inner_packet(inner_src_mac, inner_dst_mac, src.ip, dst.ip, proto,
                                    sport, dport, ip_len, ttl=inner_ttl)
        encap_frame_len = inner_frame_len + pk.VXLAN_OVERHEAD

        def encap(outer_src_mac: str, outer_dst_mac: str, ttl: int) -> bytes:
            hdr = pk.encapsulate(inner_hdr, inner_frame_len, outer_src_mac, outer_dst_mac,
                                 src_dev.vtep, dst_dev.vtep, vni, outer_sport, ttl=ttl)
            return pk.pad_header(hdr, encap_frame_len, hb)

        up = ifindex(self._uplink_port(src_dev.name, spine.name))
        hops = [
            access_hop(up),
            Hop(spine.name, ifindex(self._downlink_port(spine.name, src_dev.name)),
                ifindex(self._downlink_port(spine.name, dst_dev.name)),
                encap(src_dev.mac, spine.mac, 255), encap_frame_len + 4),
            Hop(dst_dev.name, ifindex(self._uplink_port(dst_dev.name, spine.name)),
                ifindex(dst.port), encap(spine.mac, dst_dev.mac, 254), encap_frame_len + 4,
                0, dst.vlan),
        ]
        return Path("l3vni" if routed else "l2vni", hops, vni, spine.name,
                    src_dev.vtep, dst_dev.vtep, outer_sport)

    # ------------------------------------------------------------ interfaces

    def interfaces(self) -> Dict[str, Dict[int, Dict]]:
        """Every interface the lab uses, per device: ifIndex -> {name, speed, desc}."""
        out: Dict[str, Dict[int, Dict]] = {name: {} for name in self.devices}
        for d in self.devices.values():
            for peer, port in d.uplinks.items():
                out[d.name][ifindex(port)] = {"name": port, "speed": 100_000_000_000,
                                              "desc": "uplink to %s" % peer}
            for peer, port in d.downlinks.items():
                out[d.name][ifindex(port)] = {"name": port, "speed": 100_000_000_000,
                                              "desc": "downlink to %s" % peer}
            for vrf, port in d.edge_ports.items():
                out[d.name][ifindex(port)] = {"name": port, "speed": 100_000_000_000,
                                              "desc": "fw-edge (VRF %s)" % vrf}
        for h in self.hosts.values():
            out[h.leaf][ifindex(h.port)] = {"name": h.port, "speed": 25_000_000_000,
                                            "desc": "%s (VLAN %d)" % (h.name, h.vlan)}
        return out

    # ------------------------------------------------------------ LLDP

    def lldp_neighbors(self) -> Dict[str, Dict[int, Dict]]:
        """What each switch would hear over LLDP, per device: local ifIndex -> neighbor.

        Fabric links are learned from both ends (spine downlinks, leaf uplinks), so a
        poller that walks every switch sees each link twice, once from each side, just
        like a real fabric. Servers and the firewall are neighbors too, but they don't
        answer SNMP themselves.
        """
        out: Dict[str, Dict[int, Dict]] = {name: {} for name in self.devices}

        def switch(peer: str, port: str) -> Dict:
            p = self.devices[peer]
            return {"sys_name": peer, "chassis_mac": p.mac, "port": port,
                    "port_desc": "", "mgmt_ip": p.mgmt_ip, "caps": "bridge,router",
                    "kind": "switch"}

        for d in self.devices.values():
            for peer, port in d.uplinks.items():
                n = switch(peer, self.devices[peer].downlinks[d.name])
                n["port_desc"] = "downlink to %s" % d.name
                out[d.name][ifindex(port)] = n
            for peer, port in d.downlinks.items():
                n = switch(peer, self.devices[peer].uplinks[d.name])
                n["port_desc"] = "uplink to %s" % d.name
                out[d.name][ifindex(port)] = n
            for i, (vrf, port) in enumerate(sorted(d.edge_ports.items(), key=lambda kv: kv[1])):
                out[d.name][ifindex(port)] = {
                    "sys_name": "fw-edge", "chassis_mac": self.fw_mac,
                    "port": "ethernet1/%d" % (i + 1), "port_desc": "%s inside" % vrf,
                    "mgmt_ip": None, "caps": "router", "kind": "firewall"}
        for h in self.hosts.values():
            out[h.leaf][ifindex(h.port)] = {
                "sys_name": h.name, "chassis_mac": h.mac, "port": "eth0",
                "port_desc": "eth0", "mgmt_ip": h.ip, "caps": "station", "kind": "server"}
        return out
