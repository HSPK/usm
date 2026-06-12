# `usm net`

Inspect, diagnose, and monitor this host's networking. **Read-only** — it
never changes interfaces, routes, DNS, or firewall rules, and the bare
dashboard makes no network request.

```bash
usm net                # dashboard: interfaces, gateway, DNS
usm net addr eth0      # addresses on one interface
usm net routes         # IPv4/IPv6 routing tables
usm net neigh          # ARP / neighbor table
usm net conns -w       # live table of established connections
usm net fw             # firewall status (ufw / nft / iptables)

usm net ping 1.1.1.1   # loss / RTT summary
usm net trace gnu.org  # mtr (or traceroute) report
usm net lookup host @8.8.8.8   # DNS resolution + timing
usm net mtu 1.1.1.1    # path-MTU probe (DF binary search)
usm net pubip          # public IP + geo/ASN (the only command that calls out)
usm net pubip --direct # ...ignoring any proxy env vars

usm net speed -w       # live per-interface throughput
```

## Inspect

`ls` (the default) combines `psutil` interface data (state, IPv4/IPv6, MAC,
MTU, link speed, rx/tx totals) with the default gateway (`ip route`) and DNS
servers (`resolvectl` / `/etc/resolv.conf`). `conns` lists established
connections and their owning process (run under `sudo` to see sockets owned
by other users). `fw` prints firewall status read-only.

## Diagnose

`ping`/`trace`/`mtu` wrap the system tools and summarise the result;
`lookup` times resolution via the system resolver, or a specific server with
`lookup <name> @<server>` (uses `dig`). `pubip` is the **only** subcommand
that reaches the internet (querying `ipinfo.io`); it honours proxy env vars
(`HTTPS_PROXY` / `ALL_PROXY`) by default, so behind a proxy it reports the
egress IP — pass `--direct` to bypass the proxy and show the host's own IP.

## Monitor

`speed` samples per-interface counters and shows rx/tx rates — one sample by
default, or a live view with `-w`.

## Why

Day-to-day server triage without memorising `ip` / `ss` / `resolvectl` flags,
and deliberately read-only so it's safe to run on a box you reach only over
SSH (no command can accidentally take the network down).

## Source

[`scripts/net.py`](https://github.com/HSPK/usm/blob/main/scripts/net.py)
