"""Traffic engine and sFlow exporters.

Each tick, every conversation pushes some number of packets through its path.
At every hop the device samples about 1 in sample_rate packets, and each sample
becomes an sFlow flow sample carrying that hop's packet header. Exporters batch
samples into datagrams and send them from their own management IP.
"""
from __future__ import annotations

import math
import random
import socket
import time
from typing import Dict, List, Optional, Tuple

from . import sflow
from .fabric import Endpoint, Fabric, Path

MAX_DATAGRAM = 1400


def poisson(lam: float, r: random.Random) -> int:
    if lam <= 0:
        return 0
    if lam > 30:
        return max(0, int(round(r.gauss(lam, math.sqrt(lam)))))
    l, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= r.random()
        if p <= l:
            return k
        k += 1


class Conversation:
    """One direction of one session (a fixed 5-tuple) and the path it takes."""

    __slots__ = ("flow", "src", "dst", "sport", "dport", "path", "bps", "ip_len", "phase")

    def __init__(self, flow: str, src: Endpoint, dst: Endpoint, sport: int, dport: int,
                 path: Path, bps: float, ip_len: int, phase: float):
        self.flow, self.src, self.dst = flow, src, dst
        self.sport, self.dport, self.path = sport, dport, path
        self.bps, self.ip_len, self.phase = bps, ip_len, phase


class Exporter:
    """One fabric switch's sFlow agent."""

    def __init__(self, name: str, agent_ip: str, collector: Tuple[str, int],
                 sample_rate: int, bind: bool, sock: Optional[socket.socket] = None):
        self.name, self.agent_ip, self.collector = name, agent_ip, collector
        self.sample_rate = sample_rate
        self.boot = time.time() - random.uniform(86400, 86400 * 90)  # "up for weeks"
        self.dgram_seq = 0
        self.flow_seq: Dict[int, int] = {}
        self.pool: Dict[int, int] = {}
        self.cntr_seq: Dict[int, int] = {}
        self.pending: List[bytes] = []
        self.sent_datagrams = 0
        self.sent_samples = 0
        if sock is not None:
            self.sock = sock
        else:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            if bind:
                self.sock.bind((agent_ip, 0))

    def add_pool(self, ifidx: int, packets: int) -> None:
        self.pool[ifidx] = self.pool.get(ifidx, 0) + packets

    def flow_sample(self, hop, drops: int = 0) -> None:
        seq = self.flow_seq.get(hop.in_if, 0) + 1
        self.flow_seq[hop.in_if] = seq
        records = [sflow.sampled_header(hop.header, hop.frame_len)]
        if hop.src_vlan or hop.dst_vlan:
            records.append(sflow.extended_switch(hop.src_vlan, hop.dst_vlan))
        self.pending.append(sflow.flow_sample(seq, hop.in_if, self.sample_rate,
                                              self.pool.get(hop.in_if, 0), drops,
                                              hop.in_if, hop.out_if, records))

    def counter_sample(self, ifidx: int, speed: int, c: Dict[str, int]) -> None:
        seq = self.cntr_seq.get(ifidx, 0) + 1
        self.cntr_seq[ifidx] = seq
        rec = sflow.generic_interface_counters(ifidx, speed, c["in_octets"], c["in_pkts"],
                                               c["out_octets"], c["out_pkts"])
        self.pending.append(sflow.counters_sample(seq, ifidx, [rec]))

    def send(self, d: bytes) -> None:
        try:
            self.sock.sendto(d, self.collector)
        except OSError:
            pass

    def flush(self, dry_run: bool = False) -> List[bytes]:
        """Pack pending samples into datagrams; send them now unless dry_run."""
        out: List[bytes] = []
        batch: List[bytes] = []
        size = 28
        for s in self.pending:
            if batch and size + len(s) > MAX_DATAGRAM:
                out.append(self._datagram(batch))
                batch, size = [], 28
            batch.append(s)
            size += len(s)
        if batch:
            out.append(self._datagram(batch))
        self.sent_samples += len(self.pending)
        self.pending = []
        if not dry_run:
            for d in out:
                self.send(d)
        self.sent_datagrams += len(out)
        return out

    def _datagram(self, samples: List[bytes]) -> bytes:
        self.dgram_seq += 1
        uptime = int((time.time() - self.boot) * 1000)
        return sflow.datagram(self.agent_ip, self.dgram_seq, uptime, samples)


class Engine:
    def __init__(self, fabric: Fabric, collector: Tuple[str, int], sample_rate: Optional[int] = None,
                 scale: float = 1.0, bind: bool = True, seed: int = 4789,
                 counter_interval: Optional[int] = None):
        self.fabric = fabric
        self.scale = scale
        self.rand = random.Random(seed)
        self.sample_rate = sample_rate or fabric.cfg.get("sample_rate", 1024)
        self.counter_interval = counter_interval or fabric.cfg.get("counter_interval", 20)
        self.start = time.time()
        self.last_counters = 0.0
        self.exporters: Dict[str, Exporter] = {
            d.name: Exporter(d.name, d.mgmt_ip, collector, self.sample_rate, bind)
            for d in fabric.devices.values()
        }
        self.ifaces = fabric.interfaces()
        self.counters: Dict[Tuple[str, int], Dict[str, int]] = {}
        for dev, ifs in self.ifaces.items():
            for idx in ifs:
                self.counters[(dev, idx)] = {"in_octets": 0, "in_pkts": 0,
                                             "out_octets": 0, "out_pkts": 0}
        self.baseline = self._build(fabric.cfg["flows"], "base")
        self.injects: Dict[str, List[Conversation]] = {}
        self.active: Dict[str, bool] = {}
        for name, inj in fabric.cfg.get("injects", {}).items():
            self.injects[name] = self._build(inj["flows"], name)
            self.active[name] = False

    # ------------------------------------------------------------ setup

    def _build(self, flows: List[Dict], tag: str) -> List[Conversation]:
        convs: List[Conversation] = []
        for f in flows:
            seed = "%s/%s" % (tag, f["name"])
            r = random.Random(seed)
            srcs = [e for ref in f["src"] for e in self.fabric.resolve(ref, seed + "/src")]
            dsts = [e for ref in f["dst"] for e in self.fabric.resolve(ref, seed + "/dst")]
            pairs = []
            if any(s.external for s in srcs) and len(dsts) > 1:
                # Outside clients land on one server each, the way a load balancer spreads them.
                pairs = [(s, dsts[i % len(dsts)]) for i, s in enumerate(srcs)]
            else:
                pairs = [(s, d) for s in srcs for d in dsts if s.ip != d.ip]
            sessions = int(f.get("sessions", 1))
            ip_len = int(f.get("ip_len", 1000))
            fwd_bps = float(f["mbps"]) * 1e6 / sessions
            rev_bps = float(f.get("reply_mbps", 0)) * 1e6 / sessions
            leak = bool(f.get("leak"))
            proto, dport = f.get("proto", "tcp"), int(f["dport"])
            for s, d in pairs:
                for _ in range(sessions):
                    sport = r.randrange(32768, 61000)
                    phase = r.uniform(0, 2 * math.pi)
                    p = self.fabric.path(s, d, proto, sport, dport, ip_len, leak)
                    convs.append(Conversation(f["name"], s, d, sport, dport, p, fwd_bps,
                                              ip_len, phase))
                    if rev_bps > 0 and p.kind != "dropped":
                        rp = self.fabric.path(d, s, proto, dport, sport, ip_len, leak)
                        convs.append(Conversation(f["name"] + " (reply)", d, s, dport, sport,
                                                  rp, rev_bps, ip_len, phase))
        return convs

    def set_inject(self, name: str, on: bool) -> None:
        if name not in self.injects:
            raise KeyError(name)
        self.active[name] = on

    def conversations(self) -> List[Conversation]:
        convs = list(self.baseline)
        for name, on in self.active.items():
            if on:
                convs.extend(self.injects[name])
        return convs

    # ------------------------------------------------------------ running

    def tick(self, dt: float, dry_run: bool = False) -> Dict[str, List[bytes]]:
        now = time.time()
        t = now - self.start
        r = self.rand
        for c in self.conversations():
            # Slow 10-minute swell plus a little noise, so graphs aren't flat lines.
            level = 1.0 + 0.25 * math.sin(2 * math.pi * t / 600 + c.phase) + r.gauss(0, 0.05)
            bps = max(0.0, c.bps * self.scale * level)
            pkts = bps * dt / 8 / c.ip_len
            whole = int(pkts) + (1 if r.random() < pkts - int(pkts) else 0)
            if whole == 0:
                continue
            for hop in c.path.hops:
                exp = self.exporters[hop.device]
                exp.add_pool(hop.in_if, whole)
                octets = whole * (hop.frame_len - 4)
                cin = self.counters.get((hop.device, hop.in_if))
                if cin:
                    cin["in_octets"] += octets
                    cin["in_pkts"] += whole
                cout = self.counters.get((hop.device, hop.out_if))
                if cout:
                    cout["out_octets"] += octets
                    cout["out_pkts"] += whole
                for _ in range(poisson(whole / self.sample_rate, r)):
                    exp.flow_sample(hop, drops=0)
        if now - self.last_counters >= self.counter_interval:
            self.last_counters = now
            for (dev, idx), cnt in self.counters.items():
                self.exporters[dev].counter_sample(idx, self.ifaces[dev][idx]["speed"], cnt)
        return {name: e.flush(dry_run) for name, e in self.exporters.items()}
