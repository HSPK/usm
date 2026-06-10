#!/usr/bin/env python3
"""Long-task notifier — wrap a command, get pinged when it finishes.

Channels (configure each once with `usm notify config`):
  ntfy      — ntfy.sh-compatible HTTP push (default https://ntfy.sh)
  telegram  — bot API (needs bot token + chat id)
  webhook   — generic POST {title, message} (Slack/Lark/Discord-friendly)

Examples:
  usm notify config ntfy --topic my-secret-topic
  usm notify config telegram --token 123:abc --chat-id 987654
  usm notify config webhook --url https://hooks.slack.com/...
  usm notify test                                   # send 'hello' through every configured channel
  usm notify -- python train.py                     # run + notify on exit
  usm notify --on fail -- ./long-job.sh             # only ping on non-zero exit
  usm notify --tag "model-x" -- bash -c 'sleep 5; false'
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import click
import httpx
from rich.console import Console

CONFIG_DIR = Path.home() / ".config" / "usm"
CONFIG_PATH = CONFIG_DIR / "notify.json"
console = Console()


def _load() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass


def _send_ntfy(c: dict, title: str, message: str) -> tuple[bool, str]:
    base = c.get("server", "https://ntfy.sh").rstrip("/")
    topic = c.get("topic")
    if not topic:
        return False, "ntfy: missing topic"
    headers = {"Title": title.encode("ascii", "replace").decode("ascii")}
    if c.get("priority"):
        headers["Priority"] = str(c["priority"])
    if c.get("tags"):
        headers["Tags"] = c["tags"]
    try:
        r = httpx.post(
            f"{base}/{topic}", content=message.encode(), headers=headers, timeout=10
        )
        return r.status_code < 300, f"ntfy: HTTP {r.status_code}"
    except httpx.HTTPError as e:
        return False, f"ntfy: {e}"


def _send_telegram(c: dict, title: str, message: str) -> tuple[bool, str]:
    token = c.get("token")
    chat_id = c.get("chat_id")
    if not token or not chat_id:
        return False, "telegram: missing token or chat_id"
    text = f"*{_md(title)}*\n```\n{message[-3500:]}\n```"
    try:
        r = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "MarkdownV2"},
            timeout=10,
        )
        return r.status_code < 300, f"telegram: HTTP {r.status_code}"
    except httpx.HTTPError as e:
        return False, f"telegram: {e}"


def _md(s: str) -> str:
    for ch in r"_*[]()~`>#+-=|{}.!":
        s = s.replace(ch, "\\" + ch)
    return s


def _send_webhook(c: dict, title: str, message: str) -> tuple[bool, str]:
    url = c.get("url")
    if not url:
        return False, "webhook: missing url"
    payload_tpl = c.get("payload") or {"text": f"{title}\n{message}"}
    payload = json.loads(
        json.dumps(payload_tpl)
        .replace("{title}", json.dumps(title).strip('"'))
        .replace("{message}", json.dumps(message).strip('"'))
    )
    try:
        r = httpx.post(url, json=payload, timeout=10)
        return r.status_code < 400, f"webhook: HTTP {r.status_code}"
    except httpx.HTTPError as e:
        return False, f"webhook: {e}"


SENDERS = {"ntfy": _send_ntfy, "telegram": _send_telegram, "webhook": _send_webhook}


def _broadcast(title: str, message: str) -> int:
    cfg = _load()
    if not cfg:
        console.print(
            "[yellow]usm notify: no channel configured.[/yellow] "
            "Run `usm notify config ntfy --topic ...` to set one."
        )
        return 0
    ok = 0
    for name, sender in SENDERS.items():
        sub = cfg.get(name)
        if not sub:
            continue
        success, msg = sender(sub, title, message)
        if success:
            ok += 1
            console.print(f"[green]✓[/green] {msg}")
        else:
            console.print(f"[red]✗[/red] {msg}")
    return ok


def _fmt(secs: float) -> str:
    secs = int(secs)
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m{s:02d}s"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


# CLI ---------------------------------------------------------------------


class _NotifyGroup(click.Group):
    """Group that falls through to the `run` subcommand when the first arg
    isn't a known subcommand. Lets `usm notify -- cmd args` work alongside
    `usm notify config ntfy ...` and `usm notify test`."""

    def resolve_command(self, ctx, args):
        if not args:
            return super().resolve_command(ctx, args)
        first = args[0]
        if not first.startswith("-") and self.get_command(ctx, first) is not None:
            return super().resolve_command(ctx, args)
        run = self.get_command(ctx, "run")
        assert run is not None
        return "run", run, args


@click.group(
    cls=_NotifyGroup,
    invoke_without_command=True,
    context_settings={
        "help_option_names": ["-h", "--help"],
        "ignore_unknown_options": True,
        "allow_extra_args": True,
    },
    help="Notify when a long-running command finishes.",
)
@click.pass_context
def cli(ctx):
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@cli.command(
    "run",
    context_settings={"ignore_unknown_options": True},
    short_help="Run CMD and ping every configured channel when it exits.",
    help=(
        "Run CMD and ping every configured channel when it exits.\n\n"
        "You can also drop the explicit 'run': `usm notify -- python train.py`."
    ),
)
@click.option(
    "--on",
    "on",
    type=click.Choice(["any", "fail", "success"]),
    default="any",
    show_default=True,
    help="When to send the notification.",
)
@click.option("--tag", default=None, help="Label included in the notification title.")
@click.option(
    "--tail",
    type=int,
    default=20,
    show_default=True,
    help="Lines of stderr to include in the notification body.",
)
@click.argument("cmd", nargs=-1, type=click.UNPROCESSED, required=True)
def cmd_run(on, tag, tail, cmd):
    host = socket.gethostname()
    label = tag or " ".join(cmd[:3]) + ("…" if len(cmd) > 3 else "")
    started = time.time()
    try:
        proc = subprocess.Popen(
            list(cmd),
            stdout=None,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError as e:
        raise click.ClickException(f"failed to spawn: {e}") from e
    stderr_buf: list[str] = []
    assert proc.stderr is not None
    for line in proc.stderr:
        sys.stderr.write(line)
        stderr_buf.append(line)
        if len(stderr_buf) > 2000:
            stderr_buf = stderr_buf[-1000:]
    rc = proc.wait()
    elapsed = time.time() - started
    if on == "fail" and rc == 0:
        sys.exit(rc)
    if on == "success" and rc != 0:
        sys.exit(rc)
    status = "OK" if rc == 0 else f"FAILED ({rc})"
    title = f"[{host}] {label} — {status} in {_fmt(elapsed)}"
    body = "".join(stderr_buf[-tail:]) or "(no stderr captured)"
    _broadcast(title, body)
    sys.exit(rc)


@cli.command("test", help="Send a hello message through every configured channel.")
def cmd_test():
    sent = _broadcast(
        f"[{socket.gethostname()}] usm notify — test",
        "If you see this, your channels work.",
    )
    if sent == 0:
        raise click.ClickException("No channels configured.")


# config group ------------------------------------------------------------


@cli.group("config", help="Configure notification channels.")
def cmd_config():
    pass


@cmd_config.command("show", help="Print the active config (tokens redacted).")
def cfg_show():
    cfg = _load()
    if not cfg:
        console.print("[dim]no config.[/dim]")
        return
    redacted = json.loads(json.dumps(cfg))
    for v in redacted.values():
        if isinstance(v, dict):
            for k in ("token", "url"):
                if v.get(k):
                    v[k] = v[k][:8] + "…" + v[k][-4:] if len(v[k]) > 16 else "***"
    console.print_json(json.dumps(redacted))


@cmd_config.command("clear", help="Remove a channel.")
@click.argument("channel", type=click.Choice(list(SENDERS)))
def cfg_clear(channel):
    cfg = _load()
    if channel in cfg:
        cfg.pop(channel)
        _save(cfg)
    console.print(f"[green]✓[/green] cleared {channel}")


@cmd_config.command("ntfy", help="Configure ntfy.sh channel.")
@click.option("--topic", required=True, help="ntfy topic (treat as a secret).")
@click.option("--server", default="https://ntfy.sh", show_default=True)
@click.option(
    "--priority",
    type=click.IntRange(1, 5),
    default=None,
    help="ntfy priority 1..5 (3 = default).",
)
@click.option("--tags", default=None, help="Comma-separated ntfy tags.")
def cfg_ntfy(topic, server, priority, tags):
    cfg = _load()
    cfg["ntfy"] = {"topic": topic, "server": server}
    if priority is not None:
        cfg["ntfy"]["priority"] = priority
    if tags:
        cfg["ntfy"]["tags"] = tags
    _save(cfg)
    console.print(f"[green]✓[/green] saved ntfy → {server}/{topic}")


@cmd_config.command("telegram", help="Configure Telegram bot channel.")
@click.option("--token", required=True, help="Bot token from @BotFather.")
@click.option("--chat-id", required=True, help="Chat id (use @userinfobot).")
def cfg_telegram(token, chat_id):
    cfg = _load()
    cfg["telegram"] = {"token": token, "chat_id": chat_id}
    _save(cfg)
    console.print("[green]✓[/green] saved telegram channel")


@cmd_config.command(
    "webhook", help="Configure a generic POST webhook (Slack/Lark/Discord etc.)."
)
@click.option("--url", required=True, help="Webhook URL.")
@click.option(
    "--payload",
    default=None,
    help='Optional JSON template; {title}/{message} interpolated. Defaults to {"text":"..."}.',
)
def cfg_webhook(url, payload):
    cfg = _load()
    entry: dict = {"url": url}
    if payload:
        try:
            entry["payload"] = json.loads(payload)
        except json.JSONDecodeError as e:
            raise click.BadParameter(f"--payload must be valid JSON: {e}") from e
    cfg["webhook"] = entry
    _save(cfg)
    console.print("[green]✓[/green] saved webhook channel")


if __name__ == "__main__":
    cli()
