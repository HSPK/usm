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
                                   ├─▶ runtime.yaml ─▶ mihomo ─▶ RESTful API ◀─ usm clash mode/node/test/logs
manager overrides (port/mode/tun)─┘        │
                                       127.0.0.1:7890 (http+socks)  ◀─ your apps
```

## Subcommands

| Command | What it does |
| --- | --- |
| `on` | Set this machine's system proxy to clash (auto-starts the core if needed). |
| `off` | Clear the system proxy (the core keeps running). |
| `up [NAME] [--tun] [--lan] [-p PORT]` | Start the core. If already running, hot-applies the given settings instead. |
| `down` | Stop the core (and restore system proxy). |
| `restart` | Restart the core. |
| `status` | Running state, current subscription/node, ports, toggles, traffic. |
| `use [NAME\|#]` | Switch the active subscription — **interactive menu** with no arg. |
| `sub add\|ls\|update\|rm` | Add / list (numbered) / refresh / delete subscriptions. |
| `node [GROUP] [NODE] [-l] [-t]` | Switch the active node (interactive with no arg); `-l` lists, `NAME` switches directly. |
| `mode [rule\|global\|direct]` | Get or set the routing mode. |
| `test [GROUP\|NODE] [--url U] [--timeout MS]` | Latency-test a group, a node, or every node (concurrent). |
| `tun on\|off\|status` | Toggle TUN (transparent system-wide capture). |
| `lan on\|off\|status` | Toggle `allow-lan` (let other devices use this box). |
| `port [N]` | Get or set the local mixed HTTP+SOCKS port. |
| `logs [-f] [-n N] [--level L]` | Tail the log file, or stream live via the API. |
| `conns [-w] [--close]` | Show active connections; `-w` watches live. |
| `dash [--no-open]` | Open the local web dashboard (metacubexd, served by mihomo). |
| `enable` / `disable` | Autostart at login via a systemd `--user` unit. |
| `setup [--force] [--geo-mirror URL]` | Download the mihomo binary + GeoIP/GeoSite data + web dashboard. |

The commands are grouped by purpose in `usm clash` (no args) and
`usm clash --help`: **Run**, **Subscriptions**, **Proxy**, **Network**,
**Observability**, and **Setup**.

!!! tip "Settings apply live"
    `mode`, `lan`, `tun`, and the active subscription (`sub use`) all take
    effect **immediately** when the core is running (the config is regenerated
    and hot-reloaded over the API) and are remembered
    for the next start. You don't need to `restart` after changing a setting.

!!! note "`up` is responsive"
    On the first run with a profile that has `GEOIP`/`GEOSITE` rules, the geo
    databases are fetched (see below), so startup can take a few seconds. `up`
    shows a spinner and waits for the controller to come up; if the process
    dies during startup it prints the tail of the log and fails clearly instead
    of reporting a false success.

## GeoIP / GeoSite data

Profiles that use `GEOIP,...` or `GEOSITE,...` rules need geo databases.
`usm clash` fetches them **itself** — with its own timeout, a progress
spinner, and mirror support — into `~/.cache/usm/clash/` (`GeoIP.dat` ≈19 MB,
`GeoSite.dat` ≈4 MB), so the mihomo core never has to download them and a
blocked GitHub fails fast with guidance instead of hanging.

- Source: **MetaCubeX/meta-rules-dat** GitHub releases
  (`https://github.com/MetaCubeX/meta-rules-dat/releases/download/latest/`).
- The fetch happens lazily on `up`/`enable` only when the active subscription
  has geo rules, and once fetched the files are reused. `usm clash setup`
  pre-fetches them (plus the binary and dashboard) up front.

```bash
usm clash setup              # download binary + geo data + dashboard
usm clash setup --force      # re-download everything
```

!!! warning "GitHub blocked? Use a mirror"
    If the download fails (common behind the GFW), point `USM_CLASH_GEO_BASE`
    at a mirror of that release path and retry:

    ```bash
    export USM_CLASH_GEO_BASE="https://ghproxy.net/https://github.com/MetaCubeX/meta-rules-dat/releases/download/latest"
    usm clash setup --force
    # or one-off:  usm clash setup --geo-mirror <base-url> --force
    ```

    The same base is written into the config's `geox-url`, so any fallback the
    core does also honours it. (For the dashboard, set `USM_CLASH_UI_URL`.)

## Subscriptions

```bash
usm clash sub add https://provider.example/sub?token=… --name work
usm clash sub add ./my-clash.yaml --name local      # a local Clash config file
usm clash sub ls                 # numbered list

# switch the active subscription — any of:
usm clash sub use                # interactive menu
usm clash sub use 2              # by number (from `sub ls`)
usm clash sub use work           # by name (substring is fine)
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

Remote subscriptions auto-refresh on `up`/`restart` once older than
`--interval` hours (default 12; `0` disables).

## Running & status

```bash
usm clash up                 # start with the active subscription
usm clash up work --lan      # switch subscription + allow LAN in one go
usm clash                    # status dashboard (same as `usm clash status`)
usm clash down
```

`status` shows the running state, the current subscription and node, mixed
port, mode, and the TUN / LAN / system-proxy toggles, plus live traffic:

```
status        ● running  (pid 12345)
subscription  work
node          HK-01  (PROXY)
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
speaks **HTTP and SOCKS5 on the same port**. Pick a different port at start
with `usm clash up -p 7891`.

## Switching the node, mode, latency tests

Switching node is interactive — no need to remember group or node names:

```bash
usm clash node                   # menu: pick a group (if several), then a node
usm clash node -t                # ...latency-testing the group first
usm clash node hk-02             # jump straight to a node by (partial) name
usm clash node PROXY             # open the node menu for one group
usm clash node PROXY hk-01       # fully explicit
usm clash node -l                # just list groups & nodes (don't switch)
```

When a subscription has **many** proxy-groups, `node` first shows your
switchable groups (the built-in `GLOBAL` group is hidden unless you're in
`global` mode), then the nodes within the one you pick — each with its
last-seen latency and a marker on the current choice. A bare node name like
`node hk-02` switches the **primary** group when that name is unique.

```bash
usm clash mode global            # rule | global | direct (instant via API)
usm clash node -l                # list all groups, members, current pick, delays
usm clash node -l PROXY          # just one group
usm clash test PROXY             # latency-test every node in the group
usm clash test hk-01             # test a single node
usm clash test                   # test all real nodes (concurrent)
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

`on` / `off` are the everyday switch — `on` points your OS proxy at clash
(starting the core first if needed), `off` clears it (the core keeps running):

```bash
usm clash on                  # auto-start + set the OS HTTP/HTTPS/SOCKS proxy
usm clash off                 # restore the previous OS proxy setting
usm clash lan on              # bind 0.0.0.0 so other LAN devices can use it
```

The system-proxy integration covers GNOME (`gsettings`), macOS
(`networksetup`), and Windows (registry). On any system it also writes shell
exports to `~/.cache/usm/clash/proxy.env` for terminal apps:

```bash
source ~/.cache/usm/clash/proxy.env     # http_proxy/https_proxy/all_proxy
```

## Logs, connections, dashboard

```bash
usm clash logs -n 100         # tail the log file
usm clash logs -f             # stream live logs via the API (Ctrl-C to stop)
usm clash conns               # active connections (host, chain, rule, up/down)
usm clash conns -w            # watch them live (full-screen; Ctrl-C to stop)
usm clash conns --close       # drop all connections
usm clash dash                # open the local web dashboard (metacubexd)
```

`dash` serves **metacubexd locally** — mihomo hosts it at
`http://<controller>/ui/` via `external-ui`, so there's no dependency on a
hosted site. The first `dash` (or `setup`) downloads the dashboard (~1 MB) into
`~/.cache/usm/clash/ui`; `dash` then opens the URL (with host/port/secret
pre-filled) in your browser, or just prints it with `--no-open`.

## State & files

Everything lives under `~/.cache/usm/clash/`:

- `profiles/<name>.yaml` + `<name>.json` — the config and its metadata.
- `state.json` — active subscription, port, mode, toggles, controller secret, pid.
- `runtime.yaml` — the composed config actually fed to mihomo (regenerated on
  every start from the active subscription + your overrides).
- `ui/` — the metacubexd dashboard (served at `/ui/`).
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
unit; toggle it interactively with `usm clash on` / `off`.)

## `usm clash` vs `usm proxy`

| | `usm clash` | [`usm proxy`](proxy.md) |
| --- | --- | --- |
| Role | Full **client manager** (ClashX-style) | Lightweight **server** + simple **client** |
| Config source | Subscriptions / Clash profiles | A single upstream URL + a few rules |
| Runtime control | mode, node selection, delay tests, live logs (API) | none (static) |
| TUN / system-proxy / LAN | yes | no |
| Best for | Daily driver with many nodes & rules | Turning a box into a proxy, or a quick one-off client |
