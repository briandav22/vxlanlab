# Running the lab on DigitalOcean (or from your Mac)

Kentik tells devices apart by the IP their flow comes from, so each of the six
fake switches must reach the Universal Agent from its own source IP. That
decides the layout.

## Option A (recommended): everything on one droplet

The lab and the Universal Agent share a droplet. The fake switch IPs live on
the droplet's loopback and never cross a network, so nothing can rewrite them.

```
 droplet
 ┌───────────────────────────────────────────────┐
 │ vxlanlab run  ── 192.168.250.x → 127.0.0.1:9995 ──▶ Universal Agent ──▶ Kentik
 └───────────────────────────────────────────────┘
```

1. Create an Ubuntu 24.04 droplet. The lab uses well under one vCPU at the
   default volume; size it for the Universal Agent.
2. Install the Universal Agent from the Kentik portal and confirm it listens
   on UDP 9995:
   ```bash
   sudo ss -ulnp | grep 9995
   ```
3. Get the lab and add the loopback aliases:
   ```bash
   sudo apt-get install -y git python3 iproute2
   git clone https://github.com/briandav22/vxlanlab.git && cd vxlanlab
   sudo python3 -m vxlanlab aliases --apply
   ip addr show lo        # 192.168.250.1, .2, .11, .12, .13, .14
   ```
   Aliases don't survive a reboot. Rerun the command, or use the systemd unit
   below, which adds them on start.
4. Add the six devices in Kentik (see `docs/kentik-runbook.md`, section 1.2):
   ```bash
   python3 -m vxlanlab devices
   ```
5. Run it inside tmux so you can type injects during a demo and detach after:
   ```bash
   tmux new -s lab
   sudo python3 -m vxlanlab run --collector 127.0.0.1 --port 9995 --snmp
   # --snmp answers SNMP (interfaces, LLDP) on UDP 161, which needs sudo.
   # Leave it off and drop the sudo if you only want flow.
   # type: elephant   (Enter)   ... type: elephant again to stop
   # Ctrl-b d to detach, tmux attach -t lab to come back
   ```

### Keep it running as a service

`contrib/vxlanlab.service` runs the baseline traffic forever, with SNMP on
(no injects; use a tmux session for those). Edit `WorkingDirectory` if you cloned somewhere
other than `/opt/vxlanlab`, then:

```bash
sudo cp contrib/vxlanlab.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now vxlanlab
journalctl -u vxlanlab -f
```

Stop the service before running an interactive copy, or you'll double the traffic.

## Option B: generate on your Mac, relay on the droplet

Sending straight from the Mac doesn't work: your router NATs every packet to
your public IP, so Kentik sees one device. Instead, the Mac sends everything
to a relay on the droplet, and the relay resends each datagram from the IP
written inside it (the sFlow agent address).

```
 Mac                                droplet
 vxlanlab run --source-mode single  relay :6343 ── 192.168.250.x → 127.0.0.1:9995 ──▶ UA
         └──────── UDP 6343 over the internet ─────┘
```

On the droplet:

```bash
cd vxlanlab
sudo python3 -m vxlanlab aliases --apply
python3 -m vxlanlab relay --listen 0.0.0.0:6343 --forward 127.0.0.1:9995
```

Allow UDP 6343 into the droplet from your home IP only (DigitalOcean cloud
firewall, or `sudo ufw allow from <your-ip> to any port 6343 proto udp`).

On the Mac (Python 3.8+, which macOS ships):

```bash
git clone https://github.com/briandav22/vxlanlab.git && cd vxlanlab
python3 -m vxlanlab run --source-mode single --collector <droplet-ip> --port 6343
```

The relay prints each switch as it first hears from it.

For SNMP in this layout, run the agents on the droplet next to the relay:
`sudo python3 -m vxlanlab snmp`. Names, speeds and LLDP neighbors are all
there; interface counters stay at 0 because the traffic is made on the Mac.

## Option C: all on the Mac

If you run a Universal Agent (or any collector) on the Mac itself:

```bash
sudo python3 -m vxlanlab aliases --apply     # uses ifconfig lo0 alias
python3 -m vxlanlab run --collector 127.0.0.1
sudo python3 -m vxlanlab aliases --apply --remove   # clean up afterwards
```

## Check the lab without Kentik

```bash
python3 -m vxlanlab decode --listen 127.0.0.1:9996 &
python3 -m vxlanlab run --port 9996 --duration 30
```

`decode` prints which sending IPs it heard from, the top talkers by outer
header, the top talkers after VXLAN decode, and bytes per VNI.

## Tuning

| Flag | Effect |
|---|---|
| `--scale 0.2` | One fifth of the traffic (and of the flow rate Kentik ingests) |
| `--sample-rate 4096` | Fewer samples, closer to a production fabric. Remember to match it in Kentik if you set it there |
| `--inject leak,noisy` | Start with scenarios on |
| `--topology my.json` | Your own fabric. Copy `vxlanlab/topology.json` and edit |

At the defaults the lab sends roughly 100 datagrams and 600 flow samples a
second across the six devices.
