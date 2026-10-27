"""SNMP agents for the fake switches: system, IF-MIB, LLDP-MIB.

Each switch answers SNMP v1/v2c on its own management IP, so a poller such as
the Kentik Universal Agent can learn interface names and speeds, and build an
LLDP topology from the remote neighbor tables. Interface ifIndexes are the same
ones the sFlow samples carry (Ethernet49 = 49).

Read-only, standard library only. BER is encoded and decoded by hand.
"""
from __future__ import annotations

import bisect
import select
import socket
import threading
import time
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .fabric import Fabric

# ------------------------------------------------------------------ BER

INTEGER, OCTET_STRING, NULL, OID = 0x02, 0x04, 0x05, 0x06
SEQUENCE = 0x30
IP_ADDRESS, COUNTER32, GAUGE32, TIMETICKS, COUNTER64 = 0x40, 0x41, 0x42, 0x43, 0x46
NO_SUCH_OBJECT, NO_SUCH_INSTANCE, END_OF_MIB_VIEW = 0x80, 0x81, 0x82

GET, GET_NEXT, RESPONSE, SET, GET_BULK = 0xA0, 0xA1, 0xA2, 0xA3, 0xA5

# error-status values
NO_ERROR, TOO_BIG, NO_SUCH_NAME, READ_ONLY = 0, 1, 2, 4
NOT_WRITABLE = 17

Oid = Tuple[int, ...]
Value = Tuple[int, object]   # (BER tag, python value)

MAX_RESPONSE = 16000         # stop adding GETBULK repetitions past this many bytes


def _len(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(b)]) + b


def tlv(tag: int, body: bytes) -> bytes:
    return bytes([tag]) + _len(len(body)) + body


def _int_body(v: int) -> bytes:
    return v.to_bytes(max(1, (v.bit_length() + 8) // 8), "big", signed=True)


def _uint_body(v: int) -> bytes:
    b = v.to_bytes(max(1, (v.bit_length() + 7) // 8), "big")
    return b"\x00" + b if b[0] & 0x80 else b


def _oid_body(oid: Oid) -> bytes:
    arcs = [oid[0] * 40 + oid[1]] + list(oid[2:])
    out = bytearray()
    for a in arcs:
        chunk = [a & 0x7F]
        a >>= 7
        while a:
            chunk.append(0x80 | (a & 0x7F))
            a >>= 7
        out.extend(reversed(chunk))
    return bytes(out)


def encode(tag: int, v: object) -> bytes:
    if tag == INTEGER:
        return tlv(tag, _int_body(int(v)))
    if tag in (COUNTER32, GAUGE32, TIMETICKS):
        return tlv(tag, _uint_body(int(v) & 0xFFFFFFFF))
    if tag == COUNTER64:
        return tlv(tag, _uint_body(int(v) & 0xFFFFFFFFFFFFFFFF))
    if tag in (OCTET_STRING, IP_ADDRESS):
        return tlv(tag, v.encode() if isinstance(v, str) else bytes(v))
    if tag == OID:
        return tlv(tag, _oid_body(tuple(v)))
    if tag in (NULL, NO_SUCH_OBJECT, NO_SUCH_INSTANCE, END_OF_MIB_VIEW):
        return tlv(tag, b"")
    raise ValueError("can't encode tag 0x%02x" % tag)


def decode_tlv(buf: bytes, pos: int = 0) -> Tuple[int, bytes, int]:
    """-> (tag, body, position after this TLV)."""
    tag = buf[pos]
    n = buf[pos + 1]
    pos += 2
    if n & 0x80:
        k = n & 0x7F
        n = int.from_bytes(buf[pos:pos + k], "big")
        pos += k
    if pos + n > len(buf):
        raise ValueError("truncated")
    return tag, buf[pos:pos + n], pos + n


def decode_oid(body: bytes) -> Oid:
    arcs, v = [], 0
    for b in body:
        v = (v << 7) | (b & 0x7F)
        if not b & 0x80:
            arcs.append(v)
            v = 0
    first = arcs[0]
    head = [min(first // 40, 2), first - 40 * min(first // 40, 2)]
    return tuple(head + arcs[1:])


def decode_int(body: bytes) -> int:
    return int.from_bytes(body, "big", signed=True)


def parse_oid(s: str) -> Oid:
    return tuple(int(x) for x in s.strip(".").split("."))


def fmt_oid(oid: Oid) -> str:
    return ".".join(str(x) for x in oid)


# ------------------------------------------------------------------ messages

class Request:
    def __init__(self, version: int, community: bytes, pdu_type: int, request_id: int,
                 non_repeaters: int, max_repetitions: int, oids: List[Oid]):
        self.version, self.community, self.pdu_type = version, community, pdu_type
        self.request_id, self.oids = request_id, oids
        self.non_repeaters, self.max_repetitions = non_repeaters, max_repetitions


def decode_request(buf: bytes) -> Request:
    tag, msg, _ = decode_tlv(buf)
    if tag != SEQUENCE:
        raise ValueError("not an SNMP message")
    _, ver, p = decode_tlv(msg)
    _, community, p = decode_tlv(msg, p)
    pdu_type, pdu, _ = decode_tlv(msg, p)
    _, rid, p = decode_tlv(pdu)
    _, f1, p = decode_tlv(pdu, p)
    _, f2, p = decode_tlv(pdu, p)
    _, vbl, _ = decode_tlv(pdu, p)
    oids, q = [], 0
    while q < len(vbl):
        _, vb, q = decode_tlv(vbl, q)
        _, ob, _ = decode_tlv(vb)
        oids.append(decode_oid(ob))
    return Request(decode_int(ver), community, pdu_type, decode_int(rid),
                   decode_int(f1), decode_int(f2), oids)


def encode_response(version: int, community: bytes, request_id: int,
                    varbinds: Sequence[Tuple[Oid, Value]], error_status: int = 0,
                    error_index: int = 0, pdu_type: int = RESPONSE) -> bytes:
    vbs = b"".join(tlv(SEQUENCE, encode(OID, o) + encode(*v)) for o, v in varbinds)
    pdu = tlv(pdu_type, encode(INTEGER, request_id) + encode(INTEGER, error_status)
              + encode(INTEGER, error_index) + tlv(SEQUENCE, vbs))
    return tlv(SEQUENCE, encode(INTEGER, version) + encode(OCTET_STRING, community) + pdu)


def encode_request(version: int, community: bytes, pdu_type: int, request_id: int,
                   oids: Sequence[Oid], non_repeaters: int = 0, max_repetitions: int = 0) -> bytes:
    """For tests and the self-check: build a GET / GETNEXT / GETBULK."""
    return encode_response(version, community, request_id, [(o, (NULL, None)) for o in oids],
                           non_repeaters, max_repetitions, pdu_type)


def decode_response(buf: bytes) -> Tuple[int, int, List[Tuple[Oid, Value]]]:
    """-> (error_status, error_index, [(oid, (tag, value))]). For tests and the self-check."""
    _, msg, _ = decode_tlv(buf)
    _, _, p = decode_tlv(msg)
    _, _, p = decode_tlv(msg, p)
    _, pdu, _ = decode_tlv(msg, p)
    _, _, p = decode_tlv(pdu)
    _, es, p = decode_tlv(pdu, p)
    _, ei, p = decode_tlv(pdu, p)
    _, vbl, _ = decode_tlv(pdu, p)
    out, q = [], 0
    while q < len(vbl):
        _, vb, q = decode_tlv(vbl, q)
        _, ob, r = decode_tlv(vb)
        tag, body, _ = decode_tlv(vb, r)
        if tag == OID:
            val: object = decode_oid(body)
        elif tag == INTEGER:
            val = decode_int(body)
        elif tag in (COUNTER32, GAUGE32, TIMETICKS, COUNTER64):
            val = int.from_bytes(body, "big")
        elif tag == IP_ADDRESS:
            val = socket.inet_ntoa(body)
        else:
            val = body
        out.append((decode_oid(ob), (tag, val)))
    return decode_int(es), decode_int(ei), out


# ------------------------------------------------------------------ MIB

SYSTEM = (1, 3, 6, 1, 2, 1, 1)
IF_NUMBER = (1, 3, 6, 1, 2, 1, 2, 1, 0)
IF_ENTRY = (1, 3, 6, 1, 2, 1, 2, 2, 1)
IP_ADDR_ENTRY = (1, 3, 6, 1, 2, 1, 4, 20, 1)
IFX_ENTRY = (1, 3, 6, 1, 2, 1, 31, 1, 1, 1)
LLDP = (1, 0, 8802, 1, 1, 2, 1)
LLDP_LOC = LLDP + (3,)
LLDP_LOC_PORT = LLDP_LOC + (7, 1)
LLDP_LOC_MAN_ADDR = LLDP_LOC + (8, 1)
LLDP_REM = LLDP + (4, 1, 1)
LLDP_REM_MAN_ADDR = LLDP + (4, 2, 1)

MGMT_IFINDEX = 999001         # Management1, where Arista numbers it

# LldpSystemCapabilitiesMap is a BITS string: bit 0 is the top bit of the first byte.
CAP_BITS = {"other": 0, "repeater": 1, "bridge": 2, "wlan": 3, "router": 4,
            "telephone": 5, "docsis": 6, "station": 7}

# Port ID subtypes (LLDP-MIB LldpPortIdSubtype) and chassis ID subtypes.
PORT_ID_IFNAME, PORT_ID_LOCAL = 5, 7
CHASSIS_ID_MAC = 4

DEFAULTS = {
    "community": "public",
    # Arista's enterprise number (30065), so pollers pick an Arista-style profile, which
    # fits the EthernetN port names. Override in topology.json under "snmp".
    "sys_object_id": "1.3.6.1.4.1.30065.1.3011.7050.3282.52",
    "sys_descr": "Arista Networks EOS version 4.31.2F running on an Arista Networks "
                 "DCS-7050CX3-32S (simulated by vxlanlab)",
    "sys_contact": "netops@example.com",
    "sys_location": "vxlanlab, DC1 row A",
}


def caps(spec: str) -> bytes:
    b = 0
    for name in spec.split(","):
        if name:
            b |= 0x80 >> CAP_BITS[name]
    return bytes([b, 0])


def mac_bytes(mac: str) -> bytes:
    return bytes(int(x, 16) for x in mac.split(":"))


def ip_bytes(ip: str) -> bytes:
    return socket.inet_aton(ip)


class Mib:
    """A sorted OID tree. Values are (tag, value), or a callable returning one."""

    def __init__(self):
        self._vals: Dict[Oid, object] = {}
        self._keys: List[Oid] = []

    def set(self, oid: Oid, tag: int, value: object) -> None:
        self._vals[oid] = (tag, value)

    def set_fn(self, oid: Oid, fn: Callable[[], Value]) -> None:
        self._vals[oid] = fn

    def freeze(self) -> "Mib":
        self._keys = sorted(self._vals)
        return self

    def _value(self, oid: Oid) -> Value:
        v = self._vals[oid]
        return v() if callable(v) else v  # type: ignore[return-value]

    def get(self, oid: Oid) -> Optional[Value]:
        return self._value(oid) if oid in self._vals else None

    def get_next(self, oid: Oid) -> Optional[Tuple[Oid, Value]]:
        i = bisect.bisect_right(self._keys, oid)
        if i >= len(self._keys):
            return None
        k = self._keys[i]
        return k, self._value(k)

    def has_prefix(self, prefix: Oid) -> bool:
        i = bisect.bisect_left(self._keys, prefix)
        return i < len(self._keys) and self._keys[i][:len(prefix)] == prefix

    def __len__(self) -> int:
        return len(self._keys)

    def walk(self, prefix: Oid = ()) -> List[Tuple[Oid, Value]]:
        i = bisect.bisect_left(self._keys, prefix)
        out = []
        while i < len(self._keys) and self._keys[i][:len(prefix)] == prefix:
            out.append((self._keys[i], self._value(self._keys[i])))
            i += 1
        return out


def build_mib(fabric: Fabric, name: str, boot: float,
              counters: Optional[Dict[Tuple[str, int], Dict[str, int]]] = None,
              settings: Optional[Dict] = None) -> Mib:
    """Everything one fake switch answers: system, interfaces, IP addresses, LLDP."""
    cfg = dict(DEFAULTS)
    cfg.update(settings or {})
    dev = fabric.devices[name]
    ifs = dict(fabric.interfaces()[name])
    ifs[MGMT_IFINDEX] = {"name": "Management1", "speed": 1_000_000_000,
                         "desc": "oob mgmt %s" % dev.mgmt_ip}
    neighbors = fabric.lldp_neighbors()[name]
    m = Mib()

    def uptime() -> Value:
        return TIMETICKS, int((time.time() - boot) * 100)

    # system
    m.set(SYSTEM + (1, 0), OCTET_STRING, cfg["sys_descr"])
    m.set(SYSTEM + (2, 0), OID, parse_oid(cfg["sys_object_id"]))
    m.set_fn(SYSTEM + (3, 0), uptime)
    m.set(SYSTEM + (4, 0), OCTET_STRING, cfg["sys_contact"])
    m.set(SYSTEM + (5, 0), OCTET_STRING, name)
    m.set(SYSTEM + (6, 0), OCTET_STRING, cfg["sys_location"])
    m.set(SYSTEM + (7, 0), INTEGER, 6)  # layer 2 + layer 3

    # IF-MIB ifTable and ifXTable. Counters come from the traffic engine when there is one.
    m.set(IF_NUMBER, INTEGER, len(ifs))

    def counter(idx: int, key: str, tag: int) -> Callable[[], Value]:
        def fn() -> Value:
            c = counters.get((name, idx)) if counters else None
            return tag, (c[key] if c else 0)
        return fn

    for idx, i in sorted(ifs.items()):
        speed = i["speed"]
        mac = mac_bytes(dev.mac)
        e = IF_ENTRY
        m.set(e + (1, idx), INTEGER, idx)
        m.set(e + (2, idx), OCTET_STRING, i["name"])
        m.set(e + (3, idx), INTEGER, 6)                       # ethernetCsmacd
        m.set(e + (4, idx), INTEGER, 9214 if idx != MGMT_IFINDEX else 1500)
        m.set(e + (5, idx), GAUGE32, min(speed, 0xFFFFFFFF))
        m.set(e + (6, idx), OCTET_STRING, mac)
        m.set(e + (7, idx), INTEGER, 1)                       # adminStatus up
        m.set(e + (8, idx), INTEGER, 1)                       # operStatus up
        m.set(e + (9, idx), TIMETICKS, 0)
        m.set_fn(e + (10, idx), counter(idx, "in_octets", COUNTER32))
        m.set_fn(e + (11, idx), counter(idx, "in_pkts", COUNTER32))
        for col in (13, 14, 15, 19, 20):                       # discards, errors, unknown
            m.set(e + (col, idx), COUNTER32, 0)
        m.set_fn(e + (16, idx), counter(idx, "out_octets", COUNTER32))
        m.set_fn(e + (17, idx), counter(idx, "out_pkts", COUNTER32))
        x = IFX_ENTRY
        m.set(x + (1, idx), OCTET_STRING, i["name"])
        for col in (2, 3, 4, 5, 8, 9, 12, 13):                 # multicast / broadcast
            m.set(x + (col, idx), COUNTER32 if col < 6 else COUNTER64, 0)
        m.set_fn(x + (6, idx), counter(idx, "in_octets", COUNTER64))
        m.set_fn(x + (7, idx), counter(idx, "in_pkts", COUNTER64))
        m.set_fn(x + (10, idx), counter(idx, "out_octets", COUNTER64))
        m.set_fn(x + (11, idx), counter(idx, "out_pkts", COUNTER64))
        m.set(x + (14, idx), INTEGER, 1)                      # linkUpDownTrapEnable
        m.set(x + (15, idx), GAUGE32, speed // 1_000_000)     # ifHighSpeed, Mbps
        m.set(x + (16, idx), INTEGER, 2)                      # promiscuous false
        m.set(x + (17, idx), INTEGER, 1)                      # connectorPresent true
        m.set(x + (18, idx), OCTET_STRING, i["desc"])         # ifAlias
        m.set(x + (19, idx), TIMETICKS, 0)

    # IP-MIB ipAddrTable: the mgmt IP on Management1.
    ipo = tuple(int(o) for o in dev.mgmt_ip.split("."))
    m.set(IP_ADDR_ENTRY + (1,) + ipo, IP_ADDRESS, ip_bytes(dev.mgmt_ip))
    m.set(IP_ADDR_ENTRY + (2,) + ipo, INTEGER, MGMT_IFINDEX)
    m.set(IP_ADDR_ENTRY + (3,) + ipo, IP_ADDRESS, ip_bytes("255.255.255.0"))
    m.set(IP_ADDR_ENTRY + (4,) + ipo, INTEGER, 1)

    # LLDP-MIB local system. lldpLocPortNum is the ifIndex, as on Arista and most others.
    m.set(LLDP_LOC + (1, 0), INTEGER, CHASSIS_ID_MAC)
    m.set(LLDP_LOC + (2, 0), OCTET_STRING, mac_bytes(dev.mac))
    m.set(LLDP_LOC + (3, 0), OCTET_STRING, name)
    m.set(LLDP_LOC + (4, 0), OCTET_STRING, cfg["sys_descr"])
    m.set(LLDP_LOC + (5, 0), OCTET_STRING, caps("bridge,router"))
    m.set(LLDP_LOC + (6, 0), OCTET_STRING, caps("bridge,router"))
    for idx, i in sorted(ifs.items()):
        if idx == MGMT_IFINDEX:
            continue
        m.set(LLDP_LOC_PORT + (2, idx), INTEGER, PORT_ID_IFNAME)
        m.set(LLDP_LOC_PORT + (3, idx), OCTET_STRING, i["name"])
        m.set(LLDP_LOC_PORT + (4, idx), OCTET_STRING, i["desc"])
    mi = (1, 4) + ipo   # subtype ipv4, length 4, address
    m.set(LLDP_LOC_MAN_ADDR + (3,) + mi, INTEGER, 5)
    m.set(LLDP_LOC_MAN_ADDR + (4,) + mi, INTEGER, 2)          # ifIndex
    m.set(LLDP_LOC_MAN_ADDR + (5,) + mi, INTEGER, MGMT_IFINDEX)
    m.set(LLDP_LOC_MAN_ADDR + (6,) + mi, OID, (0, 0))

    # LLDP-MIB remote table: one neighbor per port. Index is timeMark.localPort.remIndex.
    for rem_index, (idx, n) in enumerate(sorted(neighbors.items()), start=1):
        k = (0, idx, rem_index)
        r = LLDP_REM
        m.set(r + (4,) + k, INTEGER, CHASSIS_ID_MAC)
        m.set(r + (5,) + k, OCTET_STRING, mac_bytes(n["chassis_mac"]))
        m.set(r + (6,) + k, INTEGER, PORT_ID_IFNAME)
        m.set(r + (7,) + k, OCTET_STRING, n["port"])
        m.set(r + (8,) + k, OCTET_STRING, n["port_desc"])
        m.set(r + (9,) + k, OCTET_STRING, n["sys_name"])
        m.set(r + (10,) + k, OCTET_STRING, cfg["sys_descr"] if n["kind"] == "switch"
              else {"server": "Linux %s 6.8.0-45-generic x86_64" % n["sys_name"],
                    "firewall": "fw-edge perimeter firewall"}.get(n["kind"], ""))
        m.set(r + (11,) + k, OCTET_STRING, caps(n["caps"]))
        m.set(r + (12,) + k, OCTET_STRING, caps(n["caps"]))
        if n["mgmt_ip"]:
            ma = k + (1, 4) + tuple(int(o) for o in n["mgmt_ip"].split("."))
            # Our switches say their mgmt address lives on Management1; others don't say.
            rif = MGMT_IFINDEX if n["kind"] == "switch" else 0
            m.set(LLDP_REM_MAN_ADDR + (3,) + ma, INTEGER, 2 if rif else 1)
            m.set(LLDP_REM_MAN_ADDR + (4,) + ma, INTEGER, rif)
            m.set(LLDP_REM_MAN_ADDR + (5,) + ma, OID, (0, 0))
    return m.freeze()


# ------------------------------------------------------------------ agent

class Agent:
    """Answers one switch's SNMP requests. Pure logic, no sockets."""

    def __init__(self, name: str, mib: Mib, community: str):
        self.name, self.mib = name, mib
        self.community = community.encode()
        self.requests = 0

    def handle(self, buf: bytes) -> Optional[bytes]:
        try:
            req = decode_request(buf)
        except Exception:   # garbage on the port: ignore it, like a real agent
            return None
        if req.version not in (0, 1) or req.community != self.community:
            return None   # unknown version or wrong community: real agents stay silent
        self.requests += 1
        v1 = req.version == 0
        if req.pdu_type == GET:
            vbs = []
            for i, o in enumerate(req.oids):
                val = self.mib.get(o)
                if val is None:
                    if v1:
                        return self._error(req, NO_SUCH_NAME, i + 1)
                    val = (NO_SUCH_INSTANCE if self.mib.has_prefix(o[:-1]) else NO_SUCH_OBJECT,
                           None)
                vbs.append((o, val))
            return encode_response(req.version, req.community, req.request_id, vbs)
        if req.pdu_type == GET_NEXT:
            vbs = []
            for i, o in enumerate(req.oids):
                nxt = self.mib.get_next(o)
                if nxt is None:
                    if v1:
                        return self._error(req, NO_SUCH_NAME, i + 1)
                    nxt = (o, (END_OF_MIB_VIEW, None))
                vbs.append(nxt)
            return encode_response(req.version, req.community, req.request_id, vbs)
        if req.pdu_type == GET_BULK and not v1:
            return self._bulk(req)
        if req.pdu_type == SET:
            return self._error(req, NO_SUCH_NAME if v1 else NOT_WRITABLE, 1)
        return None

    def _error(self, req: Request, status: int, index: int) -> bytes:
        return encode_response(req.version, req.community, req.request_id,
                               [(o, (NULL, None)) for o in req.oids], status, index)

    def _bulk(self, req: Request) -> bytes:
        nr = max(0, min(req.non_repeaters, len(req.oids)))
        reps = max(0, req.max_repetitions)
        vbs: List[Tuple[Oid, Value]] = []
        for o in req.oids[:nr]:
            vbs.append(self.mib.get_next(o) or (o, (END_OF_MIB_VIEW, None)))
        cursors = list(req.oids[nr:])
        size = 64
        for _ in range(reps):
            if not cursors:
                break
            row = []
            done = True
            for j, o in enumerate(cursors):
                nxt = self.mib.get_next(o)
                if nxt is None:
                    row.append((o, (END_OF_MIB_VIEW, None)))
                else:
                    row.append(nxt)
                    cursors[j] = nxt[0]
                    done = False
            row_size = sum(len(encode(OID, o)) + len(encode(*v)) + 4 for o, v in row)
            if vbs and size + row_size > MAX_RESPONSE:
                break
            vbs.extend(row)
            size += row_size
            if done:
                break
        return encode_response(req.version, req.community, req.request_id, vbs)


class SnmpServer:
    """One UDP socket per fake switch, bound to its mgmt IP, served from one thread."""

    def __init__(self, fabric: Fabric, port: int = 161, community: Optional[str] = None,
                 boots: Optional[Dict[str, float]] = None,
                 counters: Optional[Dict[Tuple[str, int], Dict[str, int]]] = None,
                 listen_ip: Optional[str] = None):
        settings = fabric.cfg.get("snmp", {})
        community = community or settings.get("community") or DEFAULTS["community"]
        self.port = port
        self.agents: Dict[socket.socket, Agent] = {}
        self.sockets: List[socket.socket] = []
        self.addresses: Dict[str, str] = {}
        boots = boots or {}
        try:
            for d in fabric.devices.values():
                boot = boots.get(d.name, time.time() - 86400 * 30)
                agent = Agent(d.name, build_mib(fabric, d.name, boot, counters, settings),
                              community)
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                ip = listen_ip or d.mgmt_ip
                s.bind((ip, port))
                self.sockets.append(s)
                self.agents[s] = agent
                self.addresses[d.name] = "%s:%d" % (ip, s.getsockname()[1])
                if listen_ip:
                    break   # single-IP test mode: answer as the first switch only
        except OSError:
            self.close()
            raise
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def serve_forever(self) -> None:
        while not self._stop.is_set():
            try:
                ready, _, _ = select.select(self.sockets, [], [], 0.5)
            except (OSError, ValueError):
                return
            for s in ready:
                try:
                    buf, src = s.recvfrom(65535)
                except OSError:
                    continue
                reply = self.agents[s].handle(buf)
                if reply:
                    try:
                        s.sendto(reply, src)
                    except OSError:
                        pass

    def start(self) -> "SnmpServer":
        self._thread = threading.Thread(target=self.serve_forever, name="snmp", daemon=True)
        self._thread.start()
        return self

    def close(self) -> None:
        if hasattr(self, "_stop"):
            self._stop.set()
        for s in self.sockets:
            s.close()

    def stats(self) -> Dict[str, int]:
        return {a.name: a.requests for a in self.agents.values()}
