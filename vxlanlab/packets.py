"""Build the raw packet headers that go inside an sFlow sampled-header record.

Only the headers matter here. sFlow exports the first N bytes of a packet
(128 by default), so we build the headers, pad the rest with payload bytes,
and report the full on-the-wire length separately.
"""
from __future__ import annotations

import socket
import struct
import zlib

ETH_HDR = 14
IP_HDR = 20
UDP_HDR = 8
TCP_HDR = 20
VXLAN_HDR = 8
VXLAN_OVERHEAD = ETH_HDR + IP_HDR + UDP_HDR + VXLAN_HDR  # 50 bytes
VXLAN_PORT = 4789

PROTO_NUM = {"icmp": 1, "tcp": 6, "udp": 17}

TCP_ACK = 0x10
TCP_PSH = 0x08


def mac_bytes(mac: str) -> bytes:
    return bytes.fromhex(mac.replace(":", ""))


def ip_bytes(ip: str) -> bytes:
    return socket.inet_aton(ip)


def checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\x00"
    total = sum(struct.unpack("!%dH" % (len(data) // 2), data))
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return ~total & 0xFFFF


def ethernet(dst_mac: str, src_mac: str, ethertype: int = 0x0800) -> bytes:
    return mac_bytes(dst_mac) + mac_bytes(src_mac) + struct.pack("!H", ethertype)


def ipv4(src: str, dst: str, proto: int, total_len: int, ttl: int = 64,
         dscp: int = 0, ident: int = 0) -> bytes:
    hdr = struct.pack("!BBHHHBBH4s4s", 0x45, dscp << 2, total_len, ident & 0xFFFF,
                      0x4000, ttl, proto, 0, ip_bytes(src), ip_bytes(dst))
    return hdr[:10] + struct.pack("!H", checksum(hdr)) + hdr[12:]


def tcp(sport: int, dport: int, seq: int = 1, ack: int = 1,
        flags: int = TCP_ACK | TCP_PSH, window: int = 65535) -> bytes:
    # Checksum left at zero: collectors never validate sampled headers.
    return struct.pack("!HHIIBBHHH", sport, dport, seq & 0xFFFFFFFF, ack & 0xFFFFFFFF,
                       5 << 4, flags, window, 0, 0)


def udp(sport: int, dport: int, length: int) -> bytes:
    return struct.pack("!HHHH", sport, dport, length, 0)


def vxlan(vni: int) -> bytes:
    # Flags byte 0x08 = "VNI present", then 24 reserved bits, 24-bit VNI, 8 reserved bits.
    return struct.pack("!B3xI", 0x08, vni << 8)


def l4_header(proto: str, sport: int, dport: int, l4_len: int, seq: int = 1) -> bytes:
    if proto == "tcp":
        return tcp(sport, dport, seq=seq, ack=seq ^ 0x5A5A5A5A)
    if proto == "udp":
        return udp(sport, dport, l4_len)
    # ICMP echo request
    return struct.pack("!BBHHH", 8, 0, 0, sport, dport)


def inner_packet(src_mac: str, dst_mac: str, src_ip: str, dst_ip: str, proto: str,
                 sport: int, dport: int, ip_len: int, ttl: int = 63, dscp: int = 0,
                 seq: int = 1) -> bytes:
    """An ordinary Ethernet/IPv4/L4 packet, headers only (no payload)."""
    proto_num = PROTO_NUM[proto]
    l4 = l4_header(proto, sport, dport, ip_len - IP_HDR, seq=seq)
    return (ethernet(dst_mac, src_mac)
            + ipv4(src_ip, dst_ip, proto_num, ip_len, ttl=ttl, dscp=dscp, ident=seq)
            + l4)


def ecmp_hash(src_ip: str, dst_ip: str, proto: str, sport: int, dport: int) -> int:
    """Stand-in for a switch's 5-tuple hash. Deterministic per flow."""
    key = "%s|%s|%s|%d|%d" % (src_ip, dst_ip, proto, sport, dport)
    return zlib.crc32(key.encode())


def vxlan_source_port(h: int) -> int:
    # RFC 7348 recommends the dynamic range 49152-65535, derived from an inner hash.
    return 49152 + (h % 16384)


def encapsulate(inner_hdrs: bytes, inner_frame_len: int, outer_src_mac: str,
                outer_dst_mac: str, vtep_src: str, vtep_dst: str, vni: int,
                udp_sport: int, ttl: int = 254) -> bytes:
    """Wrap an inner frame in outer Eth/IPv4/UDP/VXLAN headers (headers only)."""
    udp_len = UDP_HDR + VXLAN_HDR + inner_frame_len
    ip_len = IP_HDR + udp_len
    return (ethernet(outer_dst_mac, outer_src_mac)
            + ipv4(vtep_src, vtep_dst, 17, ip_len, ttl=ttl)
            + udp(udp_sport, VXLAN_PORT, udp_len)
            + vxlan(vni)
            + inner_hdrs)


def pad_header(hdrs: bytes, frame_len: int, header_bytes: int) -> bytes:
    """Fill the sampled header up to header_bytes (or the frame length) with payload."""
    want = min(header_bytes, frame_len)
    if len(hdrs) >= want:
        return hdrs[:want]
    filler = bytes((i * 7 + 0x41) & 0xFF for i in range(want - len(hdrs)))
    return hdrs + filler
