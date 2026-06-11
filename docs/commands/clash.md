# `usm clash`

A ClashX-style **command-line manager** for the [mihomo](https://github.com/MetaCubeX/mihomo)
(Clash.Meta) core: subscriptions, profile switching, rule/global/direct mode,
node selection, latency tests, TUN, system-proxy, LAN sharing, live logs and
traffic, and connection inspection — all from the terminal.

`usm clash` is the **client manager** (consume a subscription, route your
traffic out). For turning a box *into* a proxy, or a quick single-upstream
client, see [`usm proxy`](proxy.md).

The mihomo binary is auto-installed to `~/.cache/usm/bin/mihomo` on first use
(no sudo, no system packages); set `$USM_MIHOMO_BIN` to use your own.

## Mental model

- **Profiles** — Clash configs, usually fetched from a **subscription** URL.
  You can have many; one is **active**.
- **The core** — a single mihomo process driven by the active profile, with a
  handful of manager-controlled overrides (port, mode, TUN, LAN, controller).
- **Runtime control** — once running, `usm clash` talks to mihomo's RESTful
  API to switch mode, select nodes, test latency, stream logs, etc.

```
subscription ─▶ profile (active) ─┐
                                   ├─▶ runtime.yaml ─▶ mihomo ─▶ RESTful API ◀─ usm clash mode/select/test/logs
manager overrides (port/mode/tun)─┘        │
                                       127.0.0.1:7890 (http+socks)  ◀─ your apps
```

## Subcommands

| Command | What it does |
| --- | --- |
| `sub add SOURCE [--name N] [--interval H]` | Add a subscription URL or a local config file. |
| `sub ls` | List profiles (active marker, type, last update, traffic, expiry). |
| `sub update [NAME]` | Re-fetch remote profiles (one or all). |
| `sub rm NAME` | Delete a profile. |
| `use NAME` | Set the active profile (hot-applies if running). |
| `up [NAME] [--tun] [--lan] [--system-proxy] [-p PORT]` | Start the core. |
| `down` | Stop the core (and restore system proxy). |
| `restart` | Restart the core. |
| `status` | Running state, ports, mode, toggles, uptime, traffic. |
| `mode [rule\|global\|direct]` | Get or set the routing mode. |
| `proxies [GROUP]` | List groups, members, current selection, last delay. |
| `select GROUP NODE` | Pick a node in a group. |
| `test [GROUP\|NODE] [--url U] [--timeout MS]` | Latency-test a group, a node, or every node. |
| `tun on\|off\|status` | Toggle TUN (transparent system-wide capture). |
| `system-proxy on\|off\|status` | Set/clear the OS HTTP/SOCKS proxy. |
| `lan on\|off\|status` | Toggle `allow-lan` (let other devices use this box). |
| `logs [-f] [-n N] [--level L]` | Tail the log file, or stream live via the API. |
| `conns [--close]` | Show (or close) active connections. |
| `dashboard` | Print a web dashboard URL wired to the running core. |
| `enable` / `disable` | Autostart at login via a systemd `--user` unit. |
| `install [--upgrade]` | Pre-download the mihomo binary. |

## Subscriptions & profiles

```bash
usm clash sub add https://provider.example/sub?token=… --name work
usm clash sub add ./my-clash.yaml --name local      # a local Clash config file
usm clash sub ls
usm clash sub update work        # re-fetch
usm clash use work               # switch active profile
```

Subscriptions are fetched with a Clash User-Agent, so providers return a
**Clash-format YAML** (this is what carries the node list, groups, and rules —
mihomo understands every node protocol inside it: ss, vmess, vless, trojan,
hysteria2, tuic, …). The `Subscription-Userinfo` header (traffic used / total,
expiry) is parsed and shown in `sub ls`.

!!! note "Use the Clash subscription link"
    Point `sub add` at the provider's **Clash / Clash.Meta** subscription URL.
    Raw base64 node-list links (the generic "v2ray" format) are not supported —
    ask your provider for the Clash link (almost all offer one).

Remote profiles auto-refresh on `up`/`restart` once older than `--interval`
hours (default 12; `0` disables).

## Running & status

```bash
usm clash up                 # start with the active profile
usm clash up work --lan      # switch profile + allow LAN in one go
usm clash status
usm clash down
```

`status` shows the running state, mixed port, controller, mode, and the
TUN / LAN / system-proxy toggles, plus live traffic totals when running:

```
status        ● running  (pid 12345)
profile       work
mode          rule
mixed port    127.0.0.1:7890
controller    127.0.0.1:9090
tun           off
allow-lan     on
system-proxy  off
autostart     off
uptime        3m12s
traffic: ↓ 1.2GiB  ↑ 88.4MiB  | active conns: 7
```

Your apps connect to the **mixed port** (`127.0.0.1:7890` by default), which
speaks **HTTP and SOCKS5 on the same port**.

## Mode, node selection, latency tests

```bash
usm clash mode global            # rule | global | direct (instant via API)
usm clash proxies                # all groups, members, current pick, last delay
usm clash proxies PROXY          # just one group
usm clash select PROXY hk-01     # choose a node
usm clash test PROXY             # latency-test every node in the group
usm clash test hk-01             # test a single node
usm clash test                   # test all nodes
```

Node selections persist across restarts (`profile.store-selected`).

## TUN — transparent, system-wide

TUN captures **all** OS traffic (not just apps that honor proxy settings) by
creating a virtual network interface. It needs `CAP_NET_ADMIN`:

```bash
sudo setcap cap_net_admin,cap_net_bind_service+ep ~/.cache/usm/bin/mihomo
usm clash tun on        # then this works without per-run sudo
usm clash tun off
```

`usm clash tun on` prints the exact `setcap` line if the capability is missing.
When TUN is on, a sane DNS block (fake-ip) is injected if your profile doesn't
already define one.

## System proxy & LAN

```bash
usm clash system-proxy on     # set the OS HTTP/HTTPS/SOCKS proxy to the core
usm clash system-proxy off    # restore the previous setting
usm clash lan on              # bind 0.0.0.0 so other LAN devices can use it
```

`system-proxy` integrates with GNOME (`gsettings`), macOS (`networksetup`), and
Windows (registry). On any system it also writes shell exports to
`~/.cache/usm/clash/proxy.env` for terminal apps:

```bash
source ~/.cache/usm/clash/proxy.env     # http_proxy/https_proxy/all_proxy
```

## Logs, connections, dashboard

```bash
usm clash logs -n 100         # tail the log file
usm clash logs -f             # stream live logs via the API (Ctrl-C to stop)
usm clash conns               # active connections (host, chain, rule, up/down)
usm clash conns --close       # drop all connections
usm clash dashboard           # prints a metacubexd URL wired to your core
```

The dashboard URL embeds your controller host/port/secret; mihomo serves the
API with CORS enabled, so the hosted UI connects straight to your local core.

## State & files

Everything lives under `~/.cache/usm/clash/`:

- `profiles/<name>.yaml` + `<name>.json` — the config and its metadata.
- `state.json` — active profile, port, mode, toggles, controller secret, pid.
- `runtime.yaml` — the composed config actually fed to mihomo (regenerated on
  every start from the active profile + your overrides).
- `mihomo.log`, `cache.db` (node-selection persistence), `proxy.env`.

`runtime.yaml` always overrides `mixed-port`, `external-controller`, `secret`,
`mode`, `allow-lan`, and `tun` so the manager stays in control regardless of
what the subscription set.

## Autostart at boot (systemd)

```bash
usm clash enable        # ~/.config/systemd/user/usm-clash.service + start now
usm clash disable
```

The unit runs `usm clash run` in the foreground, restarts on failure, and
starts after `network-online.target`. As with [`usm tunnel`](tunnel.md), user
units only start at boot once linger is on:

```bash
sudo loginctl enable-linger "$USER"
```

(System-proxy is a desktop-session setting and is not managed by the systemd
unit; toggle it interactively with `usm clash system-proxy on`.)

## `usm clash` vs `usm proxy`

| | `usm clash` | [`usm proxy`](proxy.md) |
| --- | --- | --- |
| Role | Full **client manager** (ClashX-style) | Lightweight **server** + simple **client** |
| Config source | Subscriptions / Clash profiles | A single upstream URL + a few rules |
| Runtime control | mode, node selection, delay tests, live logs (API) | none (static) |
| TUN / system-proxy / LAN | yes | no |
| Best for | Daily driver with many nodes & rules | Turning a box into a proxy, or a quick one-off client |
