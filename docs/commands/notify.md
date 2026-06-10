# `usm notify`

Wrap a command and ping every configured channel when it finishes. Useful
for "I trained overnight, did it actually succeed?" and "did the deploy
finish?" cases.

```bash
usm notify -- python train.py                       # wrap a command
usm notify --on fail -- ./long-job.sh               # ping only on non-zero
usm notify --on success -- ./flaky-deploy.sh
usm notify --tag "model-x" -- bash -c 'sleep 5; false'
```

## Channels

Configure each one once with `usm notify config <channel> ...`. Multiple
channels can be active at the same time — every notification fans out.

=== "ntfy.sh"

    Free push notifications without auth (your topic IS the secret —
    pick something unguessable).

    ```bash
    usm notify config ntfy --topic my-very-secret-topic-123
    usm notify config ntfy --topic mytopic --server https://ntfy.example.com
    usm notify config ntfy --topic mytopic --priority 4 --tags warning,robot
    ```

    Then install the
    [ntfy mobile app](https://docs.ntfy.sh/subscribe/phone/) and subscribe
    to the same topic.

=== "Telegram"

    Create a bot via [@BotFather](https://t.me/BotFather), then ask
    [@userinfobot](https://t.me/userinfobot) for your chat id.

    ```bash
    usm notify config telegram --token 123456:ABC-XYZ --chat-id 987654321
    ```

=== "Generic webhook (Slack / Lark / Discord / ...)"

    Default payload is `{"text": "<title>\n<message>"}`, which is what
    Slack / Discord-compatible webhooks expect.

    ```bash
    usm notify config webhook --url https://hooks.slack.com/services/...
    ```

    For Lark / Feishu, pass a custom payload template; `{title}` and
    `{message}` are interpolated:

    ```bash
    usm notify config webhook \
        --url https://open.feishu.cn/open-apis/bot/v2/hook/... \
        --payload '{"msg_type":"text","content":{"text":"{title}\n{message}"}}'
    ```

## Other subcommands

```bash
usm notify config show          # show active channels (tokens redacted)
usm notify config clear ntfy    # remove a channel
usm notify test                 # send "hello" through every channel
```

## What the notification contains

- **Title**: `[hostname] <tag-or-cmd-prefix> — OK|FAILED (rc) in <elapsed>`
- **Body**: the last `--tail N` lines of the wrapped command's stderr
  (default 20). The full stderr still streams to your terminal in real time
  — the notification only includes the tail.

## Config file

`~/.config/usm/notify.json` (chmod 600). Plaintext — don't share it.

## Source

[`scripts/notify.py`](https://github.com/HSPK/usm/blob/main/scripts/notify.py)
