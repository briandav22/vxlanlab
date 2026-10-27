# Kentik runbook: illuminating a VXLAN fabric

Two parts. **Setup** gets the six lab switches into Kentik with VXLAN decode
on. **Demo** walks through the use cases one at a time, each with the inject
to turn on, the Data Explorer query to run, and what to say.

Kentik facts here come from the Kentik KB:
[Using VXLAN](https://kb.kentik.com/docs/using-vxlan),
[Device-Specific Dimensions](https://kb.kentik.com/docs/device-specific-dimensions),
[Universal Agents](https://kb.kentik.com/docs/universal-agents). Anything
marked **(verify)** is my best reading and should be confirmed in the portal
or with the Kentik SE team.

---

## Part 1: Setup

### 1.1 Universal Agent

You deploy this yourself. The KB lists UDP 9995 as the agent's inbound port
for NetFlow, sFlow and IPFIX, so that's the lab's default. If your agent's
flow capability listens elsewhere, pass `--port`.

### 1.2 Add the six devices

```bash
python3 -m vxlanlab devices          # table
python3 -m vxlanlab devices --csv    # same thing, for a spreadsheet
```

For each row, add a device in Kentik:

| Field | Value |
|---|---|
| Name | `spine-01`, `spine-02`, `border-leaf`, `leaf-01`, `leaf-02`, `leaf-03` |
| Flow type | sFlow |
| Sending IP | The `Sending IP` column (192.168.250.1, .2, .14, .11, .12, .13) |
| Sample rate | 1024 (the lab also puts it in every sample) |
| Collection | Your Universal Agent |
| SNMP | Optional. On if you run the lab with `--snmp` (section 1.6): v2c, community `public`, polling IP = the sending IP |

Kentik matches flow to a device by the IP the datagrams come from. That's why
each fake switch needs its own source IP, which `aliases --apply` sets up.

### 1.3 Turn on VXLAN decode

Per the KB, decode only works with sFlow, and it's chosen by the device
**Type** in the device's general settings. There are two options:

| Device type | Inner IPs and ports land in | VNI and VTEP land in |
|---|---|---|
| **VXLAN** | VXLAN dimensions (`VXLAN VNI 0/1 IP Address`, `VXLAN VNI 0/1 Port`, `VXLAN VNI 0/1 Protocol`) | `VXLAN VNI 0/1`, `VXLAN VTEP 0/1 IP Address` |
| **sFlow Tunnel Decode** | The standard Source/Destination IP and Port dimensions | The VXLAN dimensions |

Which to use:

- **sFlow Tunnel Decode** is the one to run day to day. Every existing
  dashboard, filter, alert and custom dimension built on Source/Destination
  IP starts working on spine traffic with no changes.
- **VXLAN** is the better *teaching* view, because the standard IP
  dimensions keep showing the underlay (VTEP → VTEP) while the VXLAN
  dimensions show the inner traffic. You can put both in one query and show
  the before and after side by side. **(verify)** that the standard
  dimensions keep the outer header in this mode.

A good demo setup: put `spine-01` on **VXLAN** and everything else on
**sFlow Tunnel Decode**. Or do a true before/after: run the lab for 15
minutes with the devices on plain sFlow, then switch the type and compare the
two time ranges.

Notes from the KB worth repeating when asked:

- Decode reads the sampled header, the first 128 to 256 bytes of each packet.
  A VXLAN-encapsulated TCP packet needs 104 bytes of headers, so 128 is
  enough. The lab sends 128.
- VXLAN is the only tunnel decoded today. GRE/NVGRE and GENEVE are listed as
  future, by request.
- The VXLAN dimension values are stored in custom (UDR) fields.

### 1.4 Start the lab and check it's landing

```bash
python3 -m vxlanlab run
```

In Kentik, Data Explorer, devices = all six, dimension = Device, metric =
bits/s, last 15 minutes. All six should show traffic within a couple of
minutes. If not, see Troubleshooting at the end.

### 1.5 Make it read like a real network (optional)

- **Interface names.** Without SNMP, interfaces show by ifIndex. Ethernet1 is
  1, Ethernet49 and 50 are the spine uplinks. `devices` prints which server
  sits on which port. With SNMP on (section 1.6) Kentik picks up the names,
  speeds and descriptions itself.
- **Name the VTEPs and tenants.** A custom dimension (or tags) on the
  10.255.0.0/24 VTEP addresses turns them into leaf names, and one on
  10.10.0.0/16 vs 10.20.0.0/16 turns inner IPs into PAYMENTS vs ANALYTICS.
  Whether a populator can match on the VNI field itself is **(verify)**. If
  it can, map 50010 → PAYMENTS, 50020 → ANALYTICS, 10010 → pay-web, and so on.

| VNI | Kind | Meaning |
|---|---|---|
| 10010 | L2 | pay-web, 10.10.1.0/24, VLAN 10 |
| 10020 | L2 | pay-db, 10.10.2.0/24, VLAN 20 |
| 10025 | L2 | pay-backup, 10.10.5.0/24, VLAN 25 |
| 20030 | L2 | anl-etl, 10.20.3.0/24, VLAN 30 |
| 20040 | L2 | anl-lake, 10.20.4.0/24, VLAN 40 |
| 50010 | L3 | VRF PAYMENTS |
| 50020 | L3 | VRF ANALYTICS |

### 1.6 SNMP and the LLDP topology map (optional)

Kentik builds its topology view from LLDP neighbor tables it polls over SNMP.
The lab can serve those, so the six switches draw as a two-tier leaf-spine,
with servers and the firewall hanging off the leaves.

1. Start the lab with SNMP. UDP 161 needs root:
   ```bash
   sudo python3 -m vxlanlab run --collector 127.0.0.1 --port 9995 --snmp
   ```
   The systemd unit in `contrib/` already passes `--snmp`.
2. Check it answers, from the droplet:
   ```bash
   sudo apt-get install -y snmp
   snmpget  -v2c -c public 192.168.250.1 1.3.6.1.2.1.1.5.0         # sysName = spine-01
   snmpwalk -v2c -c public 192.168.250.1 1.0.8802.1.1.2.1.4.1.1.9  # its 4 LLDP neighbors
   python3 -m vxlanlab snmp --show                                 # the whole neighbor table
   ```
3. Make sure your Universal Agent has its SNMP capability on. **(verify)** the
   capability name in your UA's settings.
4. On each of the six devices in Kentik, turn on SNMP polling: v2c, community
   `public`, polling IP = the sending IP (192.168.250.x). The UA polls from the
   same droplet, so it reaches the loopback aliases directly.
5. Give the first poll a few minutes. Interfaces should change from bare
   ifIndexes to `Ethernet49`, `Ethernet50`, and so on, with descriptions like
   "uplink to spine-01" and 100G speeds.
6. Open the topology view and pick the six devices. Each leaf links to both
   spines on Ethernet49/50, and the leaves list their servers as neighbors.
   **(verify)** where unmanaged neighbors (servers, `fw-edge`) show up, and
   whether the view needs the devices in a site or label first.

Matching works because every name lines up. A switch's `sysName` is its
Kentik device name, and the neighbor each peer reports (`lldpRemSysName`,
`lldpRemPortId`) is that same name and port name. If you rename a device in
Kentik, keep it the same as `sysName`, or change both.

---

## Part 2: The demo

The thread through every use case: **the spines carry everything, and know
nothing**. A spine routes on the outer header, so a VXLAN-unaware tool sees a
handful of VTEP pairs on UDP 4789. Decode gives you back the servers, ports,
tenants and customers on those same links.

Query notation below: **D** = dimensions, **F** = filters, **Dev** = devices.
Metric is bits/s unless noted.

### Use case 1: "Who is using my spine?"

Inject: none.

1. **Dev** spine-01, spine-02. **D** Source IP, Destination IP, Destination
   Port. On plain sFlow (or the standard dimensions in VXLAN mode) you get
   10.255.0.x → 10.255.0.y on port 4789. Four or five rows, all "leaf to leaf".
2. Same query with decode: db-01 → db-02 on 5433, etl → lake on 8020, web →
   db-01 on 5432, and internet clients on 443.
3. **D** `VXLAN VNI 0/1`. Traffic per segment and per tenant VRF (50010 vs 50020).

Say: "Before, the best answer to 'who's on this link' was 'leaf-03'. Now it's
'the payments database replicating to its standby', and you can see what
share PAYMENTS has versus ANALYTICS."

### Use case 2: Hot spine, one elephant

Inject: `elephant` (2.5 Gbps, db-01 → backup-01, one TCP session).

1. **Dev** spine-01, spine-02. **D** Device. spine-01 jumps, spine-02 doesn't.
2. Drill into spine-01. **D** Source IP, Destination IP, Destination Port:
   10.10.2.45 → 10.10.5.50 on 10566 dominates.
3. Without decode, the same row is 10.255.0.13 → 10.255.0.12:4789, which you
   can't tell apart from every other leaf-03 → leaf-02 conversation.

Say: "ECMP hashes each conversation onto one path. One huge conversation can't
be split, so one spine runs hot while the other idles. Decode names the
conversation, and that's who you call: the backup team, not the network team."

Run `python3 -m vxlanlab paths --flow elephant` to show which spine the hash picked.

### Use case 3: Segmentation check (route leak)

Inject: start with none, then `leak`.

1. Baseline. **F** Source IP in 10.10.0.0/16 and Destination IP in
   10.20.0.0/16 (PAYMENTS → ANALYTICS). **D** Device, Source IP, Destination
   IP, Destination Port. You'll see a trickle from web-01 → etl-01 on 9092,
   only on leaf-01, and with no VNI. That's the misconfigured app hitting a
   VRF it can't route to. The leaf dropped it, and the sample shows the attempt.
2. Turn on `leak`. Same query: now web-01/02/03 → etl-02 and lake-02 appear on
   **spines**, and **D** `VXLAN VNI 0/1` shows 50020, the ANALYTICS L3 VNI.
3. Point out the rule: PAYMENTS → ANALYTICS with no VNI is an attempt.
   PAYMENTS → ANALYTICS with a VNI on a spine means it's crossing the fabric,
   and segmentation has a hole.

Say: "This is the query to save as an alert. Nothing in the underlay changed;
only the inner headers tell you two tenants just started talking."

### Use case 4: Noisy neighbor

Inject: `noisy` (ANALYTICS lake rebuild, about 1.6 Gbps over many streams).

1. **Dev** spines. **D** `VXLAN VNI 0/1`. VNI 50020 swells on *both* spines
   (many 5-tuples, so ECMP spreads it, unlike the elephant).
2. **D** Source IP, Destination IP: lake-01/02 → etl-01/02 on 50010.
3. Contrast with use case 2: one flow on one spine vs many flows on all spines.

Say: "The fabric isn't broken; one tenant is busy. Without decode, this looks
like 'everything went up'."

### Use case 5: North-south customers inside the fabric

Inject: none, then `surge`.

1. **Dev** spines. **D** Source IP, **F** Source IP not in 10.0.0.0/8. Internet
   customers (203.0.113.x, 192.0.2.x) and branch-12 users (10.50.12.x, add
   it back in) show up *on spine links*. With decode on, Kentik's geo and ASN
   enrichment applies to them like any edge traffic. **(verify)** for your
   tenant: enrichment runs on the promoted inner IPs in Tunnel Decode mode.
2. Turn on `surge`. **D** Destination IP, Destination Port, Protocol: UDP 443
   at web-01 from hundreds of sources, visible on border-leaf's edge port
   (no encapsulation) *and* on the spines (encapsulated).

Say: "Your edge already sees this. What decode adds is following it into the
fabric: which leaf, which spine, which server."

### Use case 6: Follow one conversation hop by hop

Inject: none.

1. **F** Source IP 10.10.1.21 and Destination IP 10.10.2.45 (web-01 → db-01).
   **D** Device, Input Interface, Output Interface.
2. Each connection shows up three times: leaf-01 in on 1 (web-01's access
   port, inner only) and out on 49 or 50, a spine in on 2 (from leaf-01) and
   out on 4 (to leaf-03), then leaf-03 in on 49 or 50 and out on 1 (db-01).
   web-01 keeps a pool of 8 connections, and ECMP spreads them across both
   spines. That's the path the walkthrough animates, rebuilt from flow data.

Note: Kentik counts the same packet once per device that samples it, so
summing across all devices overstates volume. Pick one vantage point (spines,
or leaf access ports) for totals.

---

## Talking points

- **The problem in one line.** Encapsulation hides the conversation from
  every device in the middle. The underlay sees VTEP → VTEP on UDP 4789.
- **Why sFlow and not NetFlow.** sFlow ships the raw packet header, so the
  inner packet is in the sample. A NetFlow/IPFIX cache on a spine is keyed on
  the outer header and has already thrown the inner one away.
- **Two vantage points.** Leaf server ports always see the real IPs (no
  tunnel yet). Spines and uplinks only show them with decode. You need both:
  ports tell you who, spines tell you where it went and what it cost.
- **What it can't do.** No latency or loss, nothing inside encrypted tunnels,
  no VM-to-VM traffic that never leaves a host, and small flows can be missed
  by sampling.

## Questions you'll get

**Does it work with NetFlow or IPFIX?** Per the KB, VXLAN decode requires sFlow.

**Arista, Cisco Nexus, Juniper?** Any switch that exports sFlow with raw
packet headers. Whether a given platform samples on fabric uplinks, and at
what header length, is per vendor. Check 128 bytes or more.

**GENEVE, NVGRE, GRE?** Not today, per the KB. VXLAN only.

**Does decode change sampling or cost?** **(verify)** with Kentik: licensing
and whether decoded devices count differently.

**Can I see the VRF name instead of a VNI number?** By mapping VNI (or inner
subnets) to names with custom dimensions. See 1.5.

---

## Troubleshooting

| Symptom | Check |
|---|---|
| No devices receiving | Is the UA listening on 9995? `sudo ss -ulnp \| grep 9995`. Is the lab sending there? Run `decode` on another port to sanity-check the lab |
| Everything lands on one device, or none match | Sending IPs. `ip addr show lo` should list 192.168.250.1/.2/.11–.14. Rerun `sudo python3 -m vxlanlab aliases --apply` |
| `Cannot assign requested address` when starting | Same: aliases missing |
| Spines show only 10.255.0.x on 4789 | Decode isn't on for that device, or it's in VXLAN mode and you're grouping by the standard IP dimensions |
| Running from a Mac to a remote UA and all traffic shows as one IP | Expected behind NAT. Use the relay (docs/digitalocean.md) |
| Volume looks 3× too high | You're summing across leaves and spines. Pick one tier |
| `Could not start SNMP on UDP 161` | Needs root (`sudo`), and the aliases. `Address already in use` means something else holds 161 on all addresses, usually `snmpd`: `sudo ss -ulnp \| grep :161`, then stop it or set its `agentAddress` to `udp:127.0.0.1:161` |
| `snmpget` times out | Lab not started with `--snmp`, or wrong community. A wrong community gets no answer at all, as on a real switch |
| Interfaces named but no topology links | SNMP polling isn't on for every switch. A link needs at least one end polled, and both ends named as in Kentik |
