# `usm proxy`

Turn a machine into a proxy other hosts dial into, **and/or** run a local
[Clash](https://github.com/MetaCubeX/mihomo) client that sends only
rule-matched traffic out through it. Both roles are driven by a single
auto-installed [mihomo](https://github.com/MetaCubeX/mihomo) (Clash.Meta)
binary, with the same persistent state, short ids, and optional systemd
autostart you get from [`usm tunnel`](tunnel.md).

## Two roles

| Role | What it does |
| --- | --- |
| `server` | Make **this** box a proxy: a mixed HTTP+SOCKS inbound (optionally password-protected) and/or an encrypted `ss://` (Shadowsocks) inbound. It forwards everything straight out (`mode: direct`). |
| `client` | Run a local mixed HTTP+SOCKS inbound for your apps; traffic that matches a rule goes out through a remote proxy, everything else stays direct. |

```
your apps ──▶ usm proxy client (127.0.0.1:7890)
                  │  rule match → PROXY
                  ▼
            usm proxy server (host:7890 / ss://host:8388)
                  │
                  ▼
              the internet
```

## Subcommands

| Command | What it does |
| --- | --- |
| `server [OPTIONS]` | Define + start a proxy server on this box. |
| `client UPSTREAM [OPTIONS]` | Define + start a client that routes through `UPSTREAM`. |
| `url <id>` | Print the connect URLs (`http://`, `socks5://`, `ss://`) for a server. |
| `ls [--prune]` | List proxies with role / route / PID / uptime / status / boot flag. |
| `start <id>` | Relaunch a stopped proxy (or `systemctl start` if enabled). |
| `stop <id\|all>` | Stop a proxy but **keep the definition** for later. |
| `restart <id>` | `stop` + `start` (or `systemctl restart` if enabled). |
| `rm <id\|all>` | Delete the definition (also disables systemd if enabled). |
| `enable <id>` | Install a `systemd --user` unit and start it. |
| `disable <id>` | Remove the unit (keeps the definition). |
| `show <id>` | Dump the definition as JSON (passwords redacted). |
| `logs <id> [-n N]` | Print the tail of the per-proxy log. |
| `install [--upgrade]` | Pre-download the mihomo binary. |

## Server

```bash
usm proxy server                              # mixed HTTP+SOCKS on 0.0.0.0:7890
usm proxy server --auth alice:s3cret          # require username/password
usm proxy server --ss                         # also expose an encrypted ss:// inbound
usm proxy server --ss --no-mixed --host 1.2.3.4   # ss:// only, advertise a public IP
```

| Flag | Default | Meaning |
| --- | --- | --- |
| `-p`, `--port N` | `7890` | Mixed HTTP+SOCKS inbound port. |
| `--mixed / --no-mixed` | `--mixed` | Expose the mixed HTTP+SOCKS inbound. |
| `--listen ADDR` | `0.0.0.0` | Bind address for the inbounds. |
| `--auth USER:PASS` | — | Require credentials on the HTTP/SOCKS inbound. |
| `--ss` | off | Also expose an encrypted Shadowsocks inbound. |
| `--ss-port N` | `8388` | Shadowsocks port (implies `--ss`). |
| `--cipher NAME` | `aes-256-gcm` | Shadowsocks cipher. |
| `--password PW` | random | Shadowsocks password (a random one is generated if omitted). |
| `--host H` | auto | Host/IP advertised by `url` (set this to your public IP). |
| `--name NAME` | next integer | Custom id. |

After starting, `usm proxy url <id>` prints ready-to-paste connect strings:

```bash
$ usm proxy url 0
http://alice:s3cret@1.2.3.4:7890
socks5://alice:s3cret@1.2.3.4:7890
ss://YWVzLTI1Ni1nY206…@1.2.3.4:8388#usm-0
```

!!! warning "Plain HTTP/SOCKS is unencrypted"
    The mixed inbound carries traffic in the clear. Expose it only on a
    trusted network, behind `--auth`, or tunneled (e.g. via
    [`usm tunnel`](tunnel.md)). For the public internet, use the `ss://`
    inbound (`--ss`), which is encrypted, and open the port in your firewall.

## Client

Feed `client` any proxy URL — including a line straight out of
`usm proxy url`:

```bash
usm proxy client http://alice:s3cret@1.2.3.4:7890       # via HTTP
usm proxy client socks5://1.2.3.4:1080                  # via SOCKS5
usm proxy client 'ss://YWVz…@1.2.3.4:8388'              # via Shadowsocks
```

Then point your apps at `http://127.0.0.1:7890` (HTTP **and** SOCKS on the
same port):

```bash
export http_proxy=http://127.0.0.1:7890 https_proxy=http://127.0.0.1:7890
curl https://ifconfig.me      # comes out of the server
```

| Flag | Default | Meaning |
| --- | --- | --- |
| `-p`, `--port N` | `7890` | Local mixed HTTP+SOCKS inbound for your apps. |
| `--listen ADDR` | `127.0.0.1` | Bind address for the local inbound. |
| `--rule RULE` | — | Extra mihomo rule (repeatable), e.g. `'DOMAIN-SUFFIX,github.com,PROXY'`. |
| `--rules-file F` | — | Read rules from a file (one per line; `#` comments ignored). |
| `--final proxy\|direct` | `proxy` | Where **unmatched** traffic goes (the trailing `MATCH` rule). |
| `--controller [ADDR]` | off | Enable mihomo's RESTful API (default `127.0.0.1:9090`). |
| `--secret S` | — | Secret for the external controller. |
| `--name NAME` | next integer | Custom id. |

### How rules are assembled

The generated `rules:` block, top to bottom, is:

1. Private / loopback ranges (`127.0.0.0/8`, `10.0.0.0/8`, `192.168.0.0/16`,
   `::1`, …) → `DIRECT`, so LAN and localhost never loop through the proxy.
2. Your `--rule` / `--rules-file` lines, verbatim, in order.
3. `MATCH,<final>` — the catch-all (`PROXY` by default, `DIRECT` with
   `--final direct`).

Two common shapes fall out of this:

=== "Everything through the server (default)"

    ```bash
    usm proxy client http://alice:s3cret@1.2.3.4:7890
    # private ranges stay direct; everything else → the server
    ```

=== "Only specific traffic through the server"

    ```bash
    usm proxy client http://1.2.3.4:7890 \
      --rule 'DOMAIN-SUFFIX,github.com,PROXY' \
      --rule 'DOMAIN-SUFFIX,openai.com,PROXY' \
      --final direct
    # only github.com / openai.com go through the server; the rest is direct
    ```

Rules use mihomo's syntax — `DOMAIN-SUFFIX`, `DOMAIN-KEYWORD`, `IP-CIDR`,
`GEOIP`, `PROCESS-NAME`, etc. See the
[mihomo rules docs](https://wiki.metacubex.one/config/rules/). The config is
validated with `mihomo -t` before launch, so a typo fails fast with the exact
parser error.

## End-to-end example

On the server (a well-connected box):

```bash
usm proxy server --ss --auth alice:s3cret --host 203.0.113.10
usm proxy url 0          # copy the ss:// line
usm proxy enable 0       # optional: autostart at boot
```

On your laptop:

```bash
usm proxy client 'ss://YWVz…@203.0.113.10:8388'
export https_proxy=http://127.0.0.1:7890
```

## Ids, state, and logs

`server` / `client` assign the next free non-negative integer as the id
(`0`, `1`, …); pass `--name foo` for a memorable one. Each proxy lives under
`~/.cache/usm/proxy/<id>/`:

- `state.json` — the definition (`stop` keeps it; `rm` deletes it).
- `config.yaml` — the mihomo config, regenerated from `state.json` on every start.
- `mihomo.log` — appended on each (re)start; read it with `usm proxy logs <id>`.

```bash
usm proxy ls
usm proxy show 0          # passwords shown as ***
usm proxy logs 0 -n 100
usm proxy rm all
```

## The mihomo binary

The first run downloads a pinned mihomo release into
`~/.cache/usm/bin/mihomo` (`chmod +x`) — no system packages, no sudo, same
mechanism as [`usm serve`](serve.md). Pre-fetch it with `usm proxy install`,
refresh with `usm proxy install --upgrade`, or point `$USM_MIHOMO_BIN` at your
own build to bypass the download entirely.

## Autostart at boot (systemd)

`usm proxy enable <id>` writes
`~/.config/systemd/user/usm-proxy-<id>.service`, runs `daemon-reload`, then
`systemctl --user enable --now`. The unit runs `usm proxy up <id>` in the
foreground, restarts on failure, and starts after `network-online.target` —
exactly like [`usm tunnel`](tunnel.md#autostart-at-boot-systemd).

!!! warning "Linger for actual boot-time start"
    User units only start at login unless linger is on:

    ```bash
    sudo loginctl enable-linger "$USER"
    ```

    `usm proxy enable` prints this hint when linger isn't set yet.

## How is this different from `usm tunnel socks`?

[`usm tunnel socks`](tunnel.md) gives you a SOCKS5 proxy over an **SSH**
connection — great when you already have SSH access and want all traffic to
exit through that host. `usm proxy` instead runs a **standalone proxy daemon**:
it supports HTTP and SOCKS together, an encrypted `ss://` transport for the
public internet, and — on the client side — Clash **rules** so only the
traffic you choose is routed through the server.
