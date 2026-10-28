# vxlanlab

A simulated VXLAN EVPN leaf-spine fabric that sends real sFlow v5 to a Kentik
Universal Agent. Six fake switches each export from their own IP, and can
answer SNMP there too (interfaces plus LLDP neighbors, so Kentik can draw the
fabric as a topology map). The
samples carry real packet headers: inner-only on server ports, and outer VTEP
headers + VXLAN + inner packet on the fabric links. That's exactly what a real
fabric sends, so Kentik's VXLAN decode has something to decode.

Pure Python 3.8+, standard library only. Runs on macOS or Linux.

```
                 spine-01 (10.0.0.1)        spine-02 (10.0.0.2)
                   |   \   \   \           /   /   /   |
   border-leaf (VTEP .14)  leaf-01 (.11)  leaf-02 (.12)  leaf-03 (.13)
        |  |                 |  |  |       |  |  |  |     |  |  |  |
   fw-edge (NAT)          web-01      web-02 web-03       db-01 etl-02
   internet + branch      db-02       etl-01 backup-01    lake-02
                          lake-01
   VRF PAYMENTS  : L3 VNI 50010, VLAN 10/20/25 -> VNI 10010/10020/10025
   VRF ANALYTICS : L3 VNI 50020, VLAN 30/40    -> VNI 20030/20040
```

The names and addresses match the fabric walkthrough in
[`course/vxlan-fabric-walkthrough.html`](course/vxlan-fabric-walkthrough.html).

## What's in here

| Path | What it is |
|---|---|
| `vxlanlab/` | The lab: fabric model, packet builder, sFlow encoder, SNMP agent, traffic engine, CLI |
| `vxlanlab/topology.json` | Devices, VRFs, VNIs, hosts, flows and demo injects. Edit to taste |
| `docs/kentik-runbook.md` | Kentik setup plus the demo runbook, use case by use case |
| `docs/digitalocean.md` | Spinning it up on a droplet next to the Universal Agent, or from your Mac |
| `docs/vxlan-deep-dive.md` | Course section: VXLAN, why visibility disappears, how Kentik restores it |
| `course/vxlan-deep-dive.html` | The same section as an interactive page, with the walkthrough merged in |
| `tests/` | `python3 -m unittest discover -s tests` |

## Quick start (on the Universal Agent host)

```bash
git clone https://github.com/briandav22/vxlanlab.git && cd vxlanlab

# 1. Give each fake switch its own source IP (192.168.250.x on loopback)
sudo python3 -m vxlanlab aliases --apply

# 2. See what to add in Kentik: one sFlow device per switch, with its sending IP
python3 -m vxlanlab devices

# 3. Start the fabric. The Universal Agent receives flow on UDP 9995.
#    --snmp also answers SNMP on UDP 161 at each switch IP (161 needs sudo)
sudo python3 -m vxlanlab run --collector 127.0.0.1 --port 9995 --snmp
```

While it runs, type an inject name and press Enter to turn a demo scenario on
or off: `leak`, `elephant`, `noisy`, `surge`. `status`, `clear` and `quit` work
too. Or start with some on: `--inject leak,elephant`.

Running from your Mac instead? The fake switch IPs won't survive your home
NAT, so run the relay on the droplet and point the Mac at it. See
[docs/digitalocean.md](docs/digitalocean.md).

## Commands

| Command | What it does |
|---|---|
| `run` | Start the fabric. `--collector`, `--port`, `--sample-rate`, `--scale`, `--inject`, `--duration`, `--source-mode bind|single`, `--snmp` (`--snmp-port`, `--community`) |
| `snmp` | SNMP only, no traffic (counters stay at 0). `--show` prints the LLDP neighbor table every switch reports |
| `devices` | Table (or `--csv`) of the six exporters to add in Kentik |
| `kentik onboard` | Create the six devices in Kentik through the API (or update them if they exist). `--dry-run` prints the payload. See the runbook, section 1.2 |
| `paths` | How every flow crosses the fabric: L2 VNI, L3 VNI, local or dropped, which spine ECMP picks, the outer VTEP header |
| `aliases` | Print, `--apply` or `--remove` the loopback aliases for the exporter IPs (Linux `ip`, macOS `ifconfig`) |
| `relay` | Receive sFlow from anywhere and resend each datagram from its agent IP. Run it on the UA host |
| `decode` | A tiny local collector. Prints top talkers by outer header (what a VXLAN-unaware tool sees) next to top talkers after decode, plus bytes per VNI |

Try `decode` before Kentik is even set up:

```bash
python3 -m vxlanlab decode --listen 127.0.0.1:9995 &
python3 -m vxlanlab run --duration 30 --inject elephant
```

```
Top talkers, OUTER headers only (what a VXLAN-unaware collector sees):
  10.255.0.13:49908 -> 10.255.0.12:4789 udp       6788.2 MB
Top talkers after VXLAN decode (inner headers):
  10.10.2.45:51103 -> 10.10.5.50:10566 tcp        6789.7 MB
```

That's the whole pitch in two lines: the fabric reports leaf-03 talking to
leaf-02. Decode says db-01 is dumping a backup to backup-01.

## Traffic model

Baseline flows (always on), all defined in `topology.json`:

| Flow | Path | Shows |
|---|---|---|
| web-health-check | web ↔ web, VLAN 10 | Bridged in L2 VNI 10010 |
| web-to-db | web-0x → db-01 :5432 | Routed in VRF PAYMENTS, L3 VNI 50010 |
| db-replication | db-01 → db-02 :5433 | L2 VNI 10020 between racks |
| internet-https | 300 clients → web :443 | North-south, enters at border-leaf, real client IPs inside the tunnel |
| branch-portal | 40 branch-12 users → web-02 :443 | Internal users, no NAT |
| etl-kafka | etl-01 → etl-02 :9092 | L2 VNI 20030 |
| etl-to-lake | etl → lake :8020 | Routed in VRF ANALYTICS, L3 VNI 50020 |
| cross-vrf-attempt | web-01 → etl-01 :9092 | A trickle dropped at leaf-01. One sample, no VNI |

Demo injects:

| Inject | What happens | What Kentik shows |
|---|---|---|
| `leak` | Route leak between VRFs. PAYMENTS web servers reach ANALYTICS | PAYMENTS→ANALYTICS IPs now on the spines, inside VNI 50020 |
| `elephant` | 2.5 Gbps db-01 → backup-01 on one TCP session | One spine runs hot; ECMP can't split one 5-tuple |
| `noisy` | ANALYTICS lake rebuild, 64 streams per server pair, about 1.6 Gbps | VNI 50020 takes over both spines |
| `surge` | About 1.2 Gbps of small UDP packets from ~500 internet sources at web-01 :443 | Outside IPs visible on spine links, inside the tunnel |

Each conversation is a fixed 5-tuple, so it hashes to one spine every time,
like real ECMP. Traffic swells on a slow 10-minute cycle so graphs aren't flat.

## How the samples are built

Ingress sampling, 1 in 1024 by default, 128-byte headers:

| Where | Header in the sample |
|---|---|
| Source leaf, server port | Ethernet / IPv4 / TCP of the original packet. VLAN in extended switch data |
| Spine, port facing the source leaf | Outer Ethernet / IPv4 (VTEP → VTEP) / UDP 4789 / VXLAN (VNI) / inner Ethernet / IPv4 / TCP |
| Destination leaf, uplink | Same as the spine, with the outer MACs rewritten |

The outer UDP source port is a hash of the inner 5-tuple, as RFC 7348
recommends, and the same hash picks the spine. Routed traffic carries the
destination VRF's L3 VNI, with leaf router MACs inside; bridged traffic keeps
the servers' MACs and the segment's L2 VNI. Every interface also sends
generic counter samples every 20 seconds.

Validated with the lab's own decoder, [goflow2](https://github.com/netsampler/goflow2)
(parses every datagram, and reports only the outer header, like any
VXLAN-unaware collector), and scapy (full Ether/IP/UDP/VXLAN/Ether/IP/TCP stack).

## SNMP and LLDP

With `run --snmp`, each switch answers SNMP v1/v2c (read-only, community
`public` by default) on UDP 161 at its own mgmt IP, the same IP its sFlow
comes from. It serves:

| MIB | What's in it |
|---|---|
| SNMPv2-MIB system | `sysName` = the device name (`leaf-01`), Arista-style `sysDescr` and `sysObjectID`, uptime matching the sFlow agent |
| IF-MIB `ifTable` / `ifXTable` | Every port the lab uses, at the ifIndex the sFlow carries (Ethernet49 = 49), with `ifName`, `ifAlias` ("uplink to spine-01", "web-01 (VLAN 10)"), speed, and live in/out octet and packet counters from the traffic engine. Plus Management1 (ifIndex 999001) |
| IP-MIB `ipAddrTable` | The mgmt IP, on Management1 |
| LLDP-MIB | Local chassis and ports, and `lldpRemTable` / `lldpRemManAddrTable`: one neighbor per port |

The LLDP neighbors are the fabric itself: each spine sees the four leaves on
Ethernet1-4, each leaf sees spine-01 on Ethernet49 and spine-02 on
Ethernet50, from both ends with matching port names. Leaves also see their
servers (`web-01` on `eth0`), and border-leaf sees `fw-edge`. Check it with:

```bash
python3 -m vxlanlab snmp --show
snmpwalk -v2c -c public 192.168.250.11 1.0.8802.1.1.2.1.4.1.1.9   # leaf-01's neighbors
```

Change the community, `sysDescr`, `sysObjectID` or location with an `snmp`
block in `topology.json`, e.g. `"snmp": {"community": "lab-ro"}`. Setting it
up in Kentik is in [docs/kentik-runbook.md](docs/kentik-runbook.md), section 1.6.

## Limits

- Only the fabric switches are simulated, all sFlow. The firewall, internet
  router and SD-WAN boxes in the walkthrough export IPFIX in real life and
  aren't sent here.
- SNMP is v1/v2c and read-only, with no traps. Servers and fw-edge appear as
  LLDP neighbors but don't answer SNMP themselves.
- Interface counters only move with `run --snmp`. In Option B (lab on a Mac,
  relay on the droplet), run `sudo python3 -m vxlanlab snmp` on the droplet:
  names and LLDP are all there, but counters stay at 0.
- Volumes, addresses and MACs are made up. Documentation and RFC 1918 ranges only.
