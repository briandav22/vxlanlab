"""Command line entry point: python3 -m vxlanlab <command>"""
from __future__ import annotations

import argparse
import collections
import json
import os
import platform
import select
import socket
import subprocess
import sys
import threading
import time
from typing import Dict, List

from . import __version__, kentik, sflow
from .snmp import SnmpServer
from .engine import Engine, Exporter
from .fabric import DEFAULT_TOPOLOGY, Fabric

PROTO_NAME = {1: "icmp", 6: "tcp", 17: "udp"}


def _fabric(args) -> Fabric:
    return Fabric(args.topology, header_bytes=getattr(args, "header_bytes", None))


# ------------------------------------------------------------------ run

HELP_TEXT = """Commands (type and press Enter):
  <name>        toggle an inject on/off, e.g. leak
  on <name>     turn an inject on        off <name>   turn it off
  clear         turn every inject off    status       show what's running
  help          this text                quit         stop the lab"""


def _control_loop(engine: Engine, stop: threading.Event) -> None:
    """Read inject commands from the terminal while the lab runs."""
    print(HELP_TEXT)
    print("Injects: " + ", ".join(engine.injects) + "\n")
    while not stop.is_set():
        try:
            ready, _, _ = select.select([sys.stdin], [], [], 0.5)
        except (OSError, ValueError):
            return
        if not ready:
            continue
        line = sys.stdin.readline()
        if not line:
            return
        words = line.strip().lower().split()
        if not words:
            continue
        cmd, arg = words[0], (words[1] if len(words) > 1 else None)
        try:
            if cmd in ("quit", "exit", "q"):
                stop.set()
            elif cmd == "help":
                print(HELP_TEXT)
            elif cmd == "status":
                _print_status(engine)
            elif cmd == "clear":
                for name in engine.injects:
                    engine.set_inject(name, False)
                print("All injects off.")
            elif cmd in ("on", "off") and arg:
                engine.set_inject(arg, cmd == "on")
                _announce(engine, arg)
            elif cmd in engine.injects:
                engine.set_inject(cmd, not engine.active[cmd])
                _announce(engine, cmd)
            else:
                print("Unknown command. Type help.")
        except KeyError:
            print("No inject named %r. Options: %s" % (arg, ", ".join(engine.injects)))


def _announce(engine: Engine, name: str) -> None:
    state = "ON" if engine.active[name] else "off"
    about = engine.fabric.cfg["injects"][name].get("about", "")
    print("[%s] inject %s is %s. %s" % (time.strftime("%H:%M:%S"), name, state, about))


def _print_status(engine: Engine) -> None:
    on = [n for n, v in engine.active.items() if v] or ["none"]
    print("Injects on: %s" % ", ".join(on))
    for e in engine.exporters.values():
        print("  %-12s %-15s datagrams=%-8d samples=%d" % (e.name, e.agent_ip,
                                                          e.sent_datagrams, e.sent_samples))


def cmd_run(args) -> int:
    fabric = _fabric(args)
    collector = (args.collector, args.port)
    bind = args.source_mode == "bind"
    try:
        engine = Engine(fabric, collector, sample_rate=args.sample_rate, scale=args.scale,
                        bind=bind)
    except OSError as e:
        print("Could not bind an exporter source IP: %s" % e, file=sys.stderr)
        print("Each fake switch sends from its own mgmt IP. Add them first:\n"
              "  sudo python3 -m vxlanlab aliases --apply\n"
              "or run with --source-mode single to send everything from this host's IP.",
              file=sys.stderr)
        return 1
    if not bind:
        # One shared socket. Kentik will see a single sending IP, so pair this with the relay.
        shared = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for e in engine.exporters.values():
            e.sock = shared
    for name in args.inject or []:
        for n in name.split(","):
            if n:
                engine.set_inject(n.strip(), True)
    snmp = None
    if args.snmp:
        try:
            snmp = SnmpServer(fabric, port=args.snmp_port, community=args.community,
                              boots={n: e.boot for n, e in engine.exporters.items()},
                              counters=engine.counters).start()
        except OSError as e:
            _snmp_bind_error(e, args.snmp_port)
            return 1

    print("vxlanlab %s: %d exporters -> %s:%d, sampling 1:%d, scale %.2fx, source mode %s"
          % (__version__, len(engine.exporters), args.collector, args.port, engine.sample_rate,
             args.scale, args.source_mode))
    for e in engine.exporters.values():
        print("  %-12s agent IP %s" % (e.name, e.agent_ip))
    on = [n for n, v in engine.active.items() if v]
    if on:
        print("Injects on: " + ", ".join(on))
    if snmp:
        print("SNMP v1/v2c on UDP %d at each agent IP, community %r (sysName, IF-MIB, LLDP-MIB)"
              % (args.snmp_port, args.community or fabric.cfg.get("snmp", {}).get("community",
                                                                                  "public")))

    stop = threading.Event()
    if sys.stdin.isatty() and not args.no_console:
        threading.Thread(target=_control_loop, args=(engine, stop), daemon=True).start()

    started = time.time()
    last_report = started
    window_dg = 0
    next_tick = time.time()
    try:
        while not stop.is_set():
            out = engine.tick(args.interval, dry_run=True)
            # Interleave exporters and spread the sends across most of the tick, the way
            # independent switches would, instead of one burst that overflows receive buffers.
            queue = []
            lists = [[(engine.exporters[n], d) for d in ds] for n, ds in out.items()]
            while any(lists):
                for lst in lists:
                    if lst:
                        queue.append(lst.pop(0))
            window_dg += len(queue)
            if not args.dry_run and queue:
                spread = args.interval * 0.8 / len(queue)
                t0 = time.time()
                for i, (exp, d) in enumerate(queue):
                    exp.send(d)
                    delay = t0 + (i + 1) * spread - time.time()
                    if delay > 0.002:
                        time.sleep(delay)
            now = time.time()
            if not args.quiet and now - last_report >= args.report:
                total = sum(e.sent_samples for e in engine.exporters.values())
                print("[%s] %.0f datagrams/s, %d samples sent so far, injects: %s"
                      % (time.strftime("%H:%M:%S"), window_dg / (now - last_report), total,
                         ", ".join(n for n, v in engine.active.items() if v) or "none"))
                last_report, window_dg = now, 0
            if args.duration and now - started >= args.duration:
                break
            next_tick += args.interval
            stop.wait(max(0.0, next_tick - time.time()))
    except KeyboardInterrupt:
        pass
    _print_status(engine)
    if snmp:
        print("SNMP requests answered: " + ", ".join("%s=%d" % kv for kv in snmp.stats().items()))
        snmp.close()
    return 0


# ------------------------------------------------------------------ snmp

def _snmp_bind_error(e: OSError, port: int) -> None:
    print("Could not start SNMP on UDP %d: %s" % (port, e), file=sys.stderr)
    if port < 1024:
        print("Port %d needs root. Run with sudo, or pick a high port with --snmp-port." % port,
              file=sys.stderr)
    print("Each switch answers on its own mgmt IP, so the aliases must exist:\n"
          "  sudo python3 -m vxlanlab aliases --apply", file=sys.stderr)


def cmd_snmp(args) -> int:
    """SNMP only, no traffic. Interface counters stay at zero; use run --snmp for live ones."""
    fabric = _fabric(args)
    if args.show:
        nbrs = fabric.lldp_neighbors()
        ifs = fabric.interfaces()
        print("%-12s %-12s %-16s %-12s %s" % ("Device", "Local port", "Neighbor", "Remote port",
                                             "Kind"))
        for dev, ports in nbrs.items():
            for idx, n in sorted(ports.items()):
                print("%-12s %-12s %-16s %-12s %s" % (dev, ifs[dev][idx]["name"], n["sys_name"],
                                                     n["port"], n["kind"]))
        return 0
    try:
        server = SnmpServer(fabric, port=args.port, community=args.community)
    except OSError as e:
        _snmp_bind_error(e, args.port)
        return 1
    community = args.community or fabric.cfg.get("snmp", {}).get("community", "public")
    print("vxlanlab %s: SNMP v1/v2c, community %r. Interface counters stay at 0 in this mode;"
          % (__version__, community))
    print("run the fabric with `run --snmp` instead to serve live counters.")
    for name, addr in server.addresses.items():
        print("  %-12s %s" % (name, addr))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    print("SNMP requests answered: " + ", ".join("%s=%d" % kv for kv in server.stats().items()))
    server.close()
    return 0


# ------------------------------------------------------------------ devices

def cmd_devices(args) -> int:
    fabric = _fabric(args)
    rate = args.sample_rate or fabric.cfg.get("sample_rate", 1024)
    if args.csv:
        print("device_name,role,sending_ip,flow_type,sample_rate,vtep_ip,loopback")
        for d in fabric.devices.values():
            print("%s,%s,%s,sFlow,%d,%s,%s" % (d.name, d.role, d.mgmt_ip, rate,
                                               d.vtep or "", d.loopback or ""))
        return 0
    print("Add each of these as a device in Kentik (flow type sFlow), attached to your")
    print("Universal Agent, with the sending IP shown. Sample rate: 1:%d.\n" % rate)
    print("%-12s %-7s %-16s %-13s %s" % ("Device", "Role", "Sending IP", "VTEP", "Interfaces"))
    ifs = fabric.interfaces()
    for d in fabric.devices.values():
        ports = ", ".join("%s=%s" % (v["name"], v["desc"]) for _, v in sorted(ifs[d.name].items()))
        print("%-12s %-7s %-16s %-13s %s" % (d.name, d.role, d.mgmt_ip, d.vtep or "-", ports))
    return 0


# ------------------------------------------------------------------ kentik

def _env_int(name: str):
    v = os.environ.get(name, "").strip()
    if not v:
        return None
    try:
        return int(v)
    except ValueError:
        sys.exit("%s=%r isn't a number" % (name, v))


def cmd_kentik_onboard(args) -> int:
    fabric = _fabric(args)
    only = [n.strip() for v in (args.device or []) for n in v.split(",") if n.strip()]
    unknown = [n for n in only if n not in fabric.devices]
    if unknown:
        print("Unknown device(s): %s. Choose from %s" % (", ".join(unknown), ", ".join(fabric.devices)),
              file=sys.stderr)
        return 2
    devices = kentik.build_devices(
        fabric, plan_id=args.plan_id, site_id=args.site_id, agent_id=args.agent_id,
        credential=args.credential, template_id=args.template_id, sample_rate=args.sample_rate,
        snmp_port=args.snmp_port, underscores=args.underscores, only=only)
    if not args.dry_run:
        missing = [flag for flag, v in (("--email / KENTIK_EMAIL", args.email),
                                        ("--token / KENTIK_API_TOKEN", args.token),
                                        ("--plan-id / KENTIK_PLAN_ID", args.plan_id)) if not v]
        if missing:
            print("Missing " + ", ".join(missing) + ". Use --dry-run to only print the payload.",
                  file=sys.stderr)
            print("Env vars must be exported in this shell (export KENTIK_PLAN_ID=1234, not just "
                  "KENTIK_PLAN_ID=1234), and sudo drops them unless you use sudo -E.", file=sys.stderr)
            return 2
    if not args.agent_id:
        print("note: no --agent-id, so no NMS/SNMP polling config is sent", file=sys.stderr)
    elif not args.credential:
        print("WARNING: no SNMP credential (--credential / KENTIK_SNMP_CREDENTIAL), so these devices get "
              "ICMP only, no SNMP.\nCreate an SNMP v2c credential (community public) in Kentik's credential "
              "vault and pass its name.", file=sys.stderr)

    if args.dry_run:
        print("# POST %s%s/batch_create" % (args.api_url, kentik.DEVICE_PATH))
        print("# (devices Kentik already has go to PUT .../batch_update with their \"id\" instead)")
        print(json.dumps({"devices": devices}, indent=2))
        return 0

    client = kentik.Client(args.email, args.token, args.api_url)
    failures = {}   # device name -> why
    try:
        create, update = kentik.split_existing(devices, client.existing_ids())
        if create:
            print("Creating %d device(s): %s" % (len(create), ", ".join(d["deviceName"] for d in create)))
            resp = client.batch_create(create)
            _show_raw(args, "batch_create", resp)
            for d in resp.get("devices", []):
                print("  created %-12s id %s" % (d.get("deviceName"), d.get("id")))
            _retry_one_by_one(client.create, kentik.batch_failures(resp, create, "deviceName"),
                              "created", failures, args)
        if update:
            print("Updating %d existing device(s): %s" % (len(update), ", ".join(d["deviceName"] for d in update)))
            resp = client.batch_update(update)
            _show_raw(args, "batch_update", resp)
            for d in resp.get("devices", []):
                print("  updated %-12s id %s" % (d.get("deviceName"), d.get("id")))
            _retry_one_by_one(client.update, kentik.batch_failures(resp, update, "id"),
                              "updated", failures, args)

        # Trust the device list, not the batch answer.
        have = client.existing_ids()
    except kentik.KentikError as e:
        print("Kentik API error: %s" % e, file=sys.stderr)
        return 1
    missing = [d["deviceName"] for d in devices if d["deviceName"] not in have]
    for name in missing:
        failures.setdefault(name, "Kentik reported no error, but the device isn't in the account's device list")
    if failures:
        print("\n%d of %d device(s) are NOT in Kentik:" % (len(failures), len(devices)), file=sys.stderr)
        for name, why in failures.items():
            print("  %-12s %s" % (name, why), file=sys.stderr)
        if any("-" in n for n in failures) and not args.underscores:
            print("If the reason is the device name, re-run with --underscores (leaf_01 instead of leaf-01).",
                  file=sys.stderr)
        return 1
    print("Verified: all %d device(s) are in Kentik. Flow from 192.168.250.x will land on them "
          "once the lab is running." % len(devices))
    return 0


def _show_raw(args, call: str, resp) -> None:
    if args.verbose:
        print("%s response:\n%s" % (call, json.dumps(resp, indent=2)))


def _retry_one_by_one(send, failed: List[Dict], verb: str, failures: Dict[str, str], args) -> None:
    """Batch calls only say which devices failed. Retry each alone to get Kentik's reason."""
    for dev in failed:
        name = dev["deviceName"]
        try:
            resp = send(dev)
            _show_raw(args, "retry of " + name, resp)
            print("  %s %-12s id %s (on retry)" % (verb, name, resp.get("device", {}).get("id")))
        except kentik.KentikError as e:
            failures[name] = str(e)


# ------------------------------------------------------------------ paths

def cmd_paths(args) -> int:
    fabric = _fabric(args)
    engine = Engine(fabric, ("127.0.0.1", 9), bind=False)
    for e in engine.exporters.values():
        e.sock.close()
    convs = list(engine.baseline)
    for name, cs in engine.injects.items():
        convs.extend(cs)
    seen = set()
    print("%-26s %-22s %-22s %-8s %-6s %-9s %s" % ("Flow", "Inner src", "Inner dst", "Path",
                                                  "VNI", "Spine", "Outer (VTEP) header"))
    for c in convs:
        if args.flow and args.flow not in c.flow:
            continue
        key = (c.flow, c.src.name if not c.src.external else "ext",
               c.dst.name if not c.dst.external else "ext")
        if key in seen and not args.all:
            continue
        seen.add(key)
        p = c.path
        outer = "%s -> %s udp %d->4789" % (p.vtep_src, p.vtep_dst, p.outer_sport) if p.vtep_src else "-"
        print("%-26s %-22s %-22s %-8s %-6s %-9s %s" % (
            c.flow[:26], "%s:%d" % (c.src.ip, c.sport), "%s:%d" % (c.dst.ip, c.dport), p.kind,
            p.vni or "-", p.spine or "-", outer))
    return 0


# ------------------------------------------------------------------ aliases

def _alias_cmds(fabric: Fabric, remove: bool) -> List[List[str]]:
    system = platform.system()
    cmds = []
    for d in fabric.devices.values():
        if system == "Darwin":
            cmds.append(["ifconfig", "lo0", "-alias" if remove else "alias", d.mgmt_ip + "/32"])
        else:
            cmds.append(["ip", "addr", "del" if remove else "add", d.mgmt_ip + "/32", "dev", "lo"])
    return cmds


def cmd_aliases(args) -> int:
    fabric = _fabric(args)
    cmds = _alias_cmds(fabric, args.remove)
    if not args.apply:
        print("# Run these as root (or rerun with --apply under sudo):")
        for c in cmds:
            print(" ".join(c))
        return 0
    rc = 0
    for c in cmds:
        try:
            r = subprocess.run(c, capture_output=True, text=True)
        except FileNotFoundError:
            print("%s not found. Install iproute2 (apt install iproute2) and retry." % c[0])
            return 1
        status = "ok" if r.returncode == 0 else (r.stderr.strip() or "failed")
        if r.returncode != 0 and "File exists" not in status and "Cannot assign" not in status:
            rc = 1
        print("%-50s %s" % (" ".join(c), status))
    return rc


# ------------------------------------------------------------------ relay

def cmd_relay(args) -> int:
    """Receive sFlow from anywhere and resend it from a local IP equal to its agent address.

    Lets you run the generator on a laptop (behind NAT) and still have Kentik see
    one sending IP per switch: run this on the Universal Agent host.
    """
    host, _, port = args.listen.rpartition(":")
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 * 1024 * 1024)
    rx.bind((host or "0.0.0.0", int(port)))
    fhost, _, fport = args.forward.rpartition(":")
    dest = (fhost, int(fport))
    tx: Dict[str, socket.socket] = {}
    counts: collections.Counter = collections.Counter()
    last = time.time()
    print("relay: listening on %s, forwarding to %s:%d from each datagram's agent IP"
          % (args.listen, dest[0], dest[1]))
    while True:
        buf, src = rx.recvfrom(65535)
        if len(buf) < 12:
            continue
        agent = socket.inet_ntoa(buf[8:12])
        s = tx.get(agent)
        if s is None:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.bind((agent, 0))
            except OSError as e:
                print("relay: cannot bind %s (%s). Run: sudo python3 -m vxlanlab aliases --apply"
                      % (agent, e))
                s.close()
                continue
            tx[agent] = s
            print("relay: new agent %s (from %s)" % (agent, src[0]))
        s.sendto(buf, dest)
        counts[agent] += 1
        if time.time() - last >= 30:
            last = time.time()
            print("relay: last 30s " + ", ".join("%s=%d" % kv for kv in sorted(counts.items())))
            counts.clear()


# ------------------------------------------------------------------ decode

def _fmt_pkt(p: Dict) -> str:
    if not p.get("src_ip"):
        return "?"
    proto = PROTO_NAME.get(p.get("proto"), str(p.get("proto")))
    s = "%s:%s -> %s:%s %s" % (p["src_ip"], p.get("sport", ""), p["dst_ip"], p.get("dport", ""), proto)
    return s


def cmd_decode(args) -> int:
    """A tiny collector: shows what a VXLAN-unaware tool sees vs what decode reveals."""
    host, _, port = args.listen.rpartition(":")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 * 1024 * 1024)
    sock.bind((host or "0.0.0.0", int(port)))
    sock.settimeout(0.5)
    outer = collections.Counter()
    inner = collections.Counter()
    vnis = collections.Counter()
    agents = collections.Counter()
    senders = collections.Counter()
    nsamples = 0
    start = last = time.time()
    print("decode: listening on %s (Ctrl-C to stop)" % args.listen)
    try:
        while True:
            try:
                buf, src = sock.recvfrom(65535)
            except socket.timeout:
                buf = None
            if buf:
                dg = sflow.decode_datagram(buf)
                if dg:
                    senders[src[0]] += 1
                    for s in dg["samples"]:
                        if s["type"] != "flow" or "packet" not in s:
                            continue
                        nsamples += 1
                        agents[dg["agent"]] += 1
                        p = s["packet"]
                        w = s["frame_length"] * s["rate"]
                        outer[_fmt_pkt(p)] += w
                        if "vni" in p:
                            vnis[p["vni"]] += w
                            inner[_fmt_pkt(p["inner"])] += w
                        else:
                            inner[_fmt_pkt(p)] += w
                        if args.verbose:
                            line = "%-15s in=%-3d out=%-3d %s" % (dg["agent"], s["in_if"], s["out_if"], _fmt_pkt(p))
                            if "vni" in p:
                                line += "  [VNI %d] inner %s" % (p["vni"], _fmt_pkt(p["inner"]))
                            print(line)
            now = time.time()
            if now - last >= args.every:
                _decode_report(now - start, nsamples, senders, agents, outer, inner, vnis, args.top)
                last = now
            if args.duration and now - start >= args.duration:
                break
    except KeyboardInterrupt:
        pass
    _decode_report(time.time() - start, nsamples, senders, agents, outer, inner, vnis, args.top)
    return 0


def _decode_report(elapsed, n, senders, agents, outer, inner, vnis, top) -> None:
    def mb(v):
        return "%.1f MB" % (v / 1e6)

    print("\n=== %d flow samples in %.0fs ===" % (n, elapsed))
    print("Sending IPs: " + ", ".join("%s(%d)" % kv for kv in sorted(senders.items())))
    print("\nTop talkers, OUTER headers only (what a VXLAN-unaware collector sees):")
    for k, v in outer.most_common(top):
        print("  %-55s %s" % (k, mb(v)))
    print("\nTop talkers after VXLAN decode (inner headers):")
    for k, v in inner.most_common(top):
        print("  %-55s %s" % (k, mb(v)))
    print("\nBytes by VNI:")
    for k, v in vnis.most_common(top):
        print("  VNI %-8d %s" % (k, mb(v)))
    sys.stdout.flush()


# ------------------------------------------------------------------ main

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="vxlanlab",
                                 description="Simulated VXLAN EVPN fabric that exports sFlow to Kentik.")
    ap.add_argument("--version", action="version", version=__version__)
    ap.add_argument("--topology", default=DEFAULT_TOPOLOGY, help="topology JSON (default: built-in)")
    sub = ap.add_subparsers(dest="cmd")

    r = sub.add_parser("run", help="start the fabric and send sFlow to a collector")
    r.add_argument("--collector", "-c", default="127.0.0.1", help="collector IP (default 127.0.0.1)")
    r.add_argument("--port", "-p", type=int, default=9995,
                   help="collector UDP port (default 9995, the Universal Agent's flow port)")
    r.add_argument("--source-mode", choices=["bind", "single"], default="bind",
                   help="bind: each switch sends from its own mgmt IP (needs aliases). "
                        "single: everything from this host's IP (use with the relay)")
    r.add_argument("--sample-rate", type=int, help="override sampling rate (default from topology)")
    r.add_argument("--header-bytes", type=int, help="sampled header size (default 128)")
    r.add_argument("--scale", type=float, default=1.0, help="multiply all traffic volumes")
    r.add_argument("--inject", action="append", help="start with injects on, e.g. --inject leak,elephant")
    r.add_argument("--duration", type=float, default=0, help="stop after N seconds (default: run forever)")
    r.add_argument("--interval", type=float, default=1.0, help="seconds per tick")
    r.add_argument("--report", type=float, default=10.0, help="seconds between status lines")
    r.add_argument("--quiet", action="store_true")
    r.add_argument("--no-console", action="store_true", help="don't read inject commands from stdin")
    r.add_argument("--dry-run", action="store_true", help="build datagrams but don't send them")
    r.add_argument("--snmp", action="store_true",
                   help="also answer SNMP on each agent IP (sysName, IF-MIB, LLDP-MIB). Port 161 needs sudo")
    r.add_argument("--snmp-port", type=int, default=161, help="SNMP UDP port (default 161)")
    r.add_argument("--community", help="SNMP community (default: topology snmp.community, else public)")
    r.set_defaults(func=cmd_run)

    sn = sub.add_parser("snmp", help="answer SNMP only (no traffic), or --show the LLDP neighbor table")
    sn.add_argument("--port", type=int, default=161, help="UDP port (default 161, needs sudo)")
    sn.add_argument("--community", help="community (default: topology snmp.community, else public)")
    sn.add_argument("--show", action="store_true", help="print what LLDP-MIB reports, then exit")
    sn.set_defaults(func=cmd_snmp)

    d = sub.add_parser("devices", help="list the exporters to add in Kentik")
    d.add_argument("--csv", action="store_true")
    d.add_argument("--sample-rate", type=int)
    d.set_defaults(func=cmd_devices)

    k = sub.add_parser("kentik", help="onboard the lab's switches into Kentik via the API")
    ksub = k.add_subparsers(dest="kentik_cmd", metavar="<action>")
    ko = ksub.add_parser("onboard", help="create (or update) one Kentik device per switch",
                         description="Create one Kentik device per fabric switch, or update it if a "
                         "device with that name exists. Credentials and IDs come from flags or env vars.")
    ko.add_argument("--email", default=os.environ.get("KENTIK_EMAIL"), help="API user email (env KENTIK_EMAIL)")
    ko.add_argument("--token", default=os.environ.get("KENTIK_API_TOKEN"),
                    help="API token (env KENTIK_API_TOKEN; prefer the env var so it stays out of shell history)")
    ko.add_argument("--api-url", default=os.environ.get("KENTIK_API_URL", kentik.API_URL),
                    help="API base URL (env KENTIK_API_URL; EU: https://grpc.api.kentik.eu)")
    ko.add_argument("--plan-id", type=int, default=_env_int("KENTIK_PLAN_ID"), help="plan ID (env KENTIK_PLAN_ID)")
    ko.add_argument("--site-id", type=int, default=_env_int("KENTIK_SITE_ID"), help="site ID (env KENTIK_SITE_ID)")
    ko.add_argument("--agent-id", default=os.environ.get("KENTIK_AGENT_ID"),
                    help="Universal Agent ID that polls the devices (env KENTIK_AGENT_ID)")
    ko.add_argument("--credential",
                    default=os.environ.get("KENTIK_SNMP_CREDENTIAL") or os.environ.get("KENTIK_CREDENTIAL_NAME"),
                    help="name of an SNMP credential in Kentik's credential vault, not the community "
                    "string (env KENTIK_SNMP_CREDENTIAL or KENTIK_CREDENTIAL_NAME)")
    ko.add_argument("--template-id", type=int, default=_env_int("KENTIK_MONITORING_TEMPLATE"),
                    help="monitoring template ID (env KENTIK_MONITORING_TEMPLATE)")
    ko.add_argument("--sample-rate", type=int, help="override sample rate (default from topology)")
    ko.add_argument("--snmp-port", type=int, default=161, help="SNMP port the agent polls (default 161)")
    ko.add_argument("--device", action="append", help="only these switches, e.g. --device leaf-01,leaf-02")
    ko.add_argument("--underscores", action="store_true",
                    help="name devices leaf_01 instead of leaf-01, if your account rejects hyphens")
    ko.add_argument("--dry-run", action="store_true", help="print the payload, don't call the API")
    ko.add_argument("--verbose", "-v", action="store_true", help="print every API response")
    ko.set_defaults(func=cmd_kentik_onboard)
    k.set_defaults(func=lambda a: (k.print_help(), 1)[1])

    pth = sub.add_parser("paths", help="show how each flow crosses the fabric (VNI, spine, VTEPs)")
    pth.add_argument("--flow", help="only flows whose name contains this")
    pth.add_argument("--all", action="store_true", help="every session, not one per flow and pair")
    pth.set_defaults(func=cmd_paths)

    a = sub.add_parser("aliases", help="add/remove the exporter source IPs on loopback")
    a.add_argument("--apply", action="store_true", help="run the commands (needs sudo)")
    a.add_argument("--remove", action="store_true", help="remove instead of add")
    a.set_defaults(func=cmd_aliases)

    rl = sub.add_parser("relay", help="re-send sFlow from each datagram's agent IP (run on the UA host)")
    rl.add_argument("--listen", default="0.0.0.0:6343", help="host:port to receive on (default 0.0.0.0:6343)")
    rl.add_argument("--forward", default="127.0.0.1:9995", help="Universal Agent host:port (default 127.0.0.1:9995)")
    rl.set_defaults(func=cmd_relay)

    dc = sub.add_parser("decode", help="tiny local collector: outer vs inner top talkers")
    dc.add_argument("--listen", default="127.0.0.1:9995")
    dc.add_argument("--every", type=float, default=15)
    dc.add_argument("--top", type=int, default=8)
    dc.add_argument("--duration", type=float, default=0)
    dc.add_argument("--verbose", "-v", action="store_true", help="print every sample")
    dc.set_defaults(func=cmd_decode)

    args = ap.parse_args(argv)
    if not getattr(args, "func", None):
        ap.print_help()
        return 1
    return args.func(args)
