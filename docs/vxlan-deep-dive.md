# VXLAN deep dive: getting the conversation back

*Course section. The interactive version is
[`course/vxlan-deep-dive.html`](../course/vxlan-deep-dive.html), which also
contains the fabric walkthrough as its second tab. The demo steps are in
[`kentik-runbook.md`](kentik-runbook.md).*

## Why this section exists

This is a use case we believe in but haven't proven with customers yet. The
claim: in an EVPN-VXLAN data center, the links between leaves and spines hide
who is talking to whom, and Kentik can bring that back if the fabric exports
sFlow. The goal here is to understand it well enough to explain it, demo it on
the lab, and test it on a customer's own fabric before we lead with it.

## VXLAN in five minutes

A modern data center fabric is two networks stacked on top of each other.

- **The underlay** is a plain routed IP network between switches. Leaves
  connect to every spine, and routing spreads traffic across all spines with
  ECMP. The underlay only knows how to reach each leaf's loopback.
- **The overlay** is what servers see: their subnets (VLANs) and their
  tenant routing tables (VRFs), stretched across any rack.

The overlay rides on the underlay inside tunnels:

| Term | What it is | In the lab |
|---|---|---|
| **VTEP** | The tunnel endpoint on each leaf. It adds and removes the outer headers | leaf-01 = 10.255.0.11, border-leaf = 10.255.0.14 |
| **VNI** | A 24-bit label in the VXLAN header saying which segment or VRF a packet belongs to | 10010 = pay-web subnet, 50010 = VRF PAYMENTS |
| **L2 VNI** | One subnet stretched across racks. Bridged traffic, original MACs kept | web-01 ↔ web-02 in 10010 |
| **L3 VNI** | A whole VRF. Used when traffic is routed between subnets | web-01 → db-01 in 50010 |
| **VRF** | A separate routing table per tenant. Traffic can't cross unless routes are leaked | PAYMENTS, ANALYTICS |
| **EVPN** | The BGP control plane that tells every leaf which MAC or IP lives behind which VTEP | (implied) |

When web-01 sends a packet to db-01, leaf-01's VTEP wraps the whole thing in
50 bytes of new headers:

```
| outer Eth | outer IP 10.255.0.11 → 10.255.0.13 | UDP 50706 → 4789 | VXLAN VNI 50010 | inner Eth | inner IP 10.10.1.21 → 10.10.2.45 | TCP 39474 → 5432 | data |
 \_____________________ underlay sees this ______________________/   \__________________ the conversation that matters ___________________/
```

The outer UDP source port is a hash of the inner headers, so different
conversations between the same two leaves take different spines.

## Where visibility goes

Follow the packet and ask what a flow record at each point would show:

| Vantage point | What's in the packet | What a normal collector reports |
|---|---|---|
| leaf-01, port facing web-01 | Original packet only | web-01 → db-01 on 5432. Correct |
| leaf-01 uplink, spine, leaf-03 uplink | Outer + VXLAN + inner | 10.255.0.11 → 10.255.0.13 on UDP 4789 |
| leaf-03, port facing db-01 | Original packet only | web-01 → db-01 on 5432. Correct |

So the edges of the fabric are fine, and the middle, where all the shared
capacity is, reports every conversation as "leaf to leaf on 4789". A spine
carrying 400 Gbps shows a few dozen VTEP pairs. You can see that a link is
busy, never why.

That matters because the middle is where the questions are:

- Which tenant, app or host is filling this spine?
- Why is one spine hot and the other idle?
- Are two VRFs that should be isolated talking across the fabric?
- Where did that internet traffic go after the firewall?

## Why sFlow is the key

There are two ways a switch can export traffic data:

- **NetFlow / IPFIX** keeps a flow cache keyed on the headers the switch
  forwards on, and exports summaries. A spine forwards on the outer header,
  so that's what it caches. The inner packet is never recorded.
- **sFlow** copies the first bytes of 1 in N packets, raw, and sends them.
  Those bytes include the outer headers, the VXLAN header, and the inner
  packet. A VXLAN TCP packet needs 104 bytes of headers; the usual sampled
  header length is 128.

So with sFlow, the inner conversation is already in the data the spine sends.
It just needs a collector that reads past UDP 4789.

## What Kentik does

From the Kentik KB ([Using VXLAN](https://kb.kentik.com/docs/using-vxlan),
[Device-Specific Dimensions](https://kb.kentik.com/docs/device-specific-dimensions)):

- VXLAN decode requires the device's flow type to be **sFlow**.
- It reads the sampled header (128 to 256 bytes) and is turned on per device
  by setting the device **Type**:
  - **sFlow Tunnel Decode** promotes inner IPs and ports into the standard
    Source/Destination dimensions. VNI and VTEP go in VXLAN dimensions.
  - **VXLAN** puts inner fields in dedicated VXLAN dimensions (`VXLAN VNI 0/1
    IP Address`, `Port`, `Protocol`, `MAC Address`, `DSCP`, `TCP Flags`,
    `IP TTL`, plus `VXLAN VNI 0/1` and `VXLAN VTEP 0/1 IP Address`), stored as
    custom fields.
- VXLAN is the only tunnel type decoded. GRE/NVGRE and GENEVE are future.

With decode on, the spine's records read like the access ports' records, plus
the VNI and the VTEPs. Everything else Kentik does with IPs then applies:
custom dimensions (tenant names, apps), filters, saved views, alerting, and
enrichment of outside addresses.

## What it answers

| Question | Without decode | With decode | Lab inject |
|---|---|---|---|
| Who is using this spine? | VTEP pairs on 4789 | Servers, ports, tenants | none |
| Why is one spine hot? | One leaf pair is busy | One 2.5 Gbps backup session that ECMP can't split | `elephant` |
| Are the VRFs isolated? | Looks normal | PAYMENTS → ANALYTICS on the spines in VNI 50020 | `leak` |
| Which tenant is eating capacity? | Everything went up | VNI 50020 doubled: a lake rebuild | `noisy` |
| Where did internet traffic go? | border-leaf → leaf-01 | Customer IPs to web-01, with geo and ASN | `surge` |
| What path does this connection take? | Only visible at the ports | Port → uplink → spine → leaf → port | none |

## What it can't answer

- Latency, loss or jitter. Flow counts traffic; it doesn't time it.
- Anything inside encryption (IPsec, SD-WAN, the payload of TLS).
- VM-to-VM traffic that never leaves the host.
- Every small flow. Fabrics sample 1 in 4096 or more.
- True totals from summing every device: the same packet is sampled at the
  source leaf, the spine and the destination leaf. Pick one tier.

## Testing the hypothesis with a customer

Ask:

1. Do you run EVPN-VXLAN? Which vendor, how many leaves and spines?
2. Do the fabric switches export sFlow, or only NetFlow/IPFIX?
3. Is sFlow on for spines and leaf uplinks, not just server ports? What header length?
4. Where does the VNI-to-tenant mapping live?
5. What can't you answer about the fabric today?

Then prove it on their data: two spines and a leaf pair on sFlow Tunnel
Decode, "who is using this spine" before and after, VNIs mapped to names,
and one of their real questions answered live.

Still to confirm with the Kentik SE team: licensing for decoded devices,
whether custom dimension populators can match on VNI, and whether geo/ASN
enrichment applies to promoted inner IPs.

## Glossary

**ECMP**: equal-cost multipath. Spreads conversations over parallel links by
hashing headers. One conversation always takes one path.
**Overlay / underlay**: the tenant network servers see, and the switch
network that carries it.
**Sampled header**: the raw first bytes of a packet that sFlow exports.
**VTEP**: VXLAN tunnel endpoint. **VNI**: VXLAN network identifier.
**VRF**: virtual routing and forwarding table.
