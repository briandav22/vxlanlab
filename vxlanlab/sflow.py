"""sFlow version 5 encoder and decoder (the parts this lab needs).

Spec: https://sflow.org/sflow_version_5.txt

Encoded structures:
  datagram            version 5, IPv4 agent address
  flow_sample         enterprise 0, format 1
    sampled_header    enterprise 0, format 1   (raw packet header, Ethernet)
    extended_switch   enterprise 0, format 1001 (VLANs)
  counters_sample     enterprise 0, format 2
    generic_interface enterprise 0, format 1
"""
from __future__ import annotations

import socket
import struct
from typing import Dict, List, Optional

SFLOW_VERSION = 5
FMT_FLOW_SAMPLE = 1
FMT_COUNTER_SAMPLE = 2
FMT_SAMPLED_HEADER = 1
FMT_EXTENDED_SWITCH = 1001
FMT_GENERIC_IF = 1
HEADER_PROTO_ETHERNET = 1


def _record(fmt: int, body: bytes) -> bytes:
    return struct.pack("!II", fmt, len(body)) + body


def sampled_header(header: bytes, frame_length: int, stripped: int = 4) -> bytes:
    pad = (4 - len(header) % 4) % 4
    body = struct.pack("!IIII", HEADER_PROTO_ETHERNET, frame_length, stripped, len(header))
    return _record(FMT_SAMPLED_HEADER, body + header + b"\x00" * pad)


def extended_switch(src_vlan: int, dst_vlan: int, src_pri: int = 0, dst_pri: int = 0) -> bytes:
    return _record(FMT_EXTENDED_SWITCH, struct.pack("!IIII", src_vlan, src_pri, dst_vlan, dst_pri))


def flow_sample(seq: int, source_ifindex: int, sampling_rate: int, sample_pool: int,
                drops: int, input_if: int, output_if: int, records: List[bytes]) -> bytes:
    source_id = (0 << 24) | source_ifindex
    body = struct.pack("!IIIIIIII", seq & 0xFFFFFFFF, source_id, sampling_rate,
                       sample_pool & 0xFFFFFFFF, drops, input_if, output_if, len(records))
    return _record(FMT_FLOW_SAMPLE, body + b"".join(records))


def generic_interface_counters(ifindex: int, speed_bps: int, in_octets: int, in_pkts: int,
                               out_octets: int, out_pkts: int) -> bytes:
    body = struct.pack("!IIQII", ifindex, 6, speed_bps, 1, 3)
    body += struct.pack("!QIIIIII", in_octets & 0xFFFFFFFFFFFFFFFF, in_pkts & 0xFFFFFFFF,
                        0, 0, 0, 0, 0)
    # ifOut* (octets + 5 counters) followed by ifPromiscuousMode.
    body += struct.pack("!QIIIIII", out_octets & 0xFFFFFFFFFFFFFFFF, out_pkts & 0xFFFFFFFF,
                        0, 0, 0, 0, 0)
    return _record(FMT_GENERIC_IF, body)


def counters_sample(seq: int, source_ifindex: int, records: List[bytes]) -> bytes:
    body = struct.pack("!III", seq & 0xFFFFFFFF, source_ifindex, len(records))
    return _record(FMT_COUNTER_SAMPLE, body + b"".join(records))


def datagram(agent_ip: str, seq: int, uptime_ms: int, samples: List[bytes],
             sub_agent_id: int = 0) -> bytes:
    hdr = struct.pack("!II4sIIII", SFLOW_VERSION, 1, socket.inet_aton(agent_ip),
                      sub_agent_id, seq & 0xFFFFFFFF, uptime_ms & 0xFFFFFFFF, len(samples))
    return hdr + b"".join(samples)


# ---------------------------------------------------------------- decoding

def _ip(b: bytes) -> str:
    return socket.inet_ntoa(b)


def _mac(b: bytes) -> str:
    return ":".join("%02x" % x for x in b)


def decode_ethernet(data: bytes) -> Dict:
    """Decode Ethernet/IPv4/L4, descending into VXLAN when UDP dst port is 4789."""
    out: Dict = {}
    if len(data) < 14:
        return out
    out["dst_mac"], out["src_mac"] = _mac(data[0:6]), _mac(data[6:12])
    etype = struct.unpack("!H", data[12:14])[0]
    off = 14
    if etype == 0x8100 and len(data) >= 18:
        out["vlan"] = struct.unpack("!H", data[14:16])[0] & 0x0FFF
        etype = struct.unpack("!H", data[16:18])[0]
        off = 18
    if etype != 0x0800 or len(data) < off + 20:
        return out
    ihl = (data[off] & 0x0F) * 4
    out["ip_len"] = struct.unpack("!H", data[off + 2:off + 4])[0]
    out["ttl"] = data[off + 8]
    out["proto"] = data[off + 9]
    out["src_ip"], out["dst_ip"] = _ip(data[off + 12:off + 16]), _ip(data[off + 16:off + 20])
    l4 = off + ihl
    if out["proto"] in (6, 17) and len(data) >= l4 + 4:
        out["sport"], out["dport"] = struct.unpack("!HH", data[l4:l4 + 4])
        if out["proto"] == 6 and len(data) >= l4 + 14:
            out["tcp_flags"] = data[l4 + 13]
    if out.get("proto") == 17 and out.get("dport") == 4789 and len(data) >= l4 + 16:
        vx = data[l4 + 8:l4 + 16]
        out["vni"] = struct.unpack("!I", vx[4:8])[0] >> 8
        out["inner"] = decode_ethernet(data[l4 + 16:])
    return out


def decode_datagram(buf: bytes) -> Optional[Dict]:
    if len(buf) < 28:
        return None
    version, addr_type = struct.unpack("!II", buf[0:8])
    if version != SFLOW_VERSION or addr_type != 1:
        return None
    agent = _ip(buf[8:12])
    sub_agent, seq, uptime, n = struct.unpack("!IIII", buf[12:28])
    off = 28
    samples = []
    for _ in range(n):
        fmt, length = struct.unpack("!II", buf[off:off + 8])
        body = buf[off + 8:off + 8 + length]
        off += 8 + length
        if fmt == FMT_FLOW_SAMPLE:
            samples.append(_decode_flow_sample(body))
        elif fmt == FMT_COUNTER_SAMPLE:
            samples.append(_decode_counter_sample(body))
        else:
            samples.append({"type": "unknown", "format": fmt})
    return {"agent": agent, "sub_agent": sub_agent, "seq": seq, "uptime": uptime,
            "samples": samples}


def _decode_flow_sample(body: bytes) -> Dict:
    seq, source_id, rate, pool, drops, in_if, out_if, n = struct.unpack("!IIIIIIII", body[:32])
    s = {"type": "flow", "seq": seq, "source_id": source_id, "rate": rate, "pool": pool,
         "drops": drops, "in_if": in_if, "out_if": out_if}
    off = 32
    for _ in range(n):
        fmt, length = struct.unpack("!II", body[off:off + 8])
        rec = body[off + 8:off + 8 + length]
        off += 8 + length
        if fmt == FMT_SAMPLED_HEADER:
            proto, frame_len, stripped, hlen = struct.unpack("!IIII", rec[:16])
            s["frame_length"] = frame_len
            s["header_length"] = hlen
            s["packet"] = decode_ethernet(rec[16:16 + hlen])
        elif fmt == FMT_EXTENDED_SWITCH:
            s["src_vlan"], _, s["dst_vlan"], _ = struct.unpack("!IIII", rec[:16])
    return s


def _decode_counter_sample(body: bytes) -> Dict:
    seq, source_id, n = struct.unpack("!III", body[:12])
    s = {"type": "counters", "seq": seq, "source_id": source_id, "interfaces": []}
    off = 12
    for _ in range(n):
        fmt, length = struct.unpack("!II", body[off:off + 8])
        rec = body[off + 8:off + 8 + length]
        off += 8 + length
        if fmt == FMT_GENERIC_IF:
            ifindex, iftype, speed, direction, status = struct.unpack("!IIQII", rec[:24])
            in_oct, in_pkts = struct.unpack("!QI", rec[24:36])
            out_oct, out_pkts = struct.unpack("!QI", rec[56:68])
            s["interfaces"].append({"ifindex": ifindex, "speed": speed, "in_octets": in_oct,
                                    "in_pkts": in_pkts, "out_octets": out_oct,
                                    "out_pkts": out_pkts})
    return s
