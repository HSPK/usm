# `usm dl`

Download a large file over a connection you do not trust, and be able to
prove you got it intact.

```bash
usm dl https://example.com/model.safetensors
usm dl https://example.com/big.tar -o /data/            # into a directory
usm dl URL --sha256 9f86d0...                            # verify
usm dl URL --checksum-file URL.sha256                    # verify from a list
usm dl URL --mirror https://mirror2/... --mirror https://mirror3/...
usm dl URL --dry-run                                     # size, name, resumability
```

## Resuming

Downloads land in `<name>.part` and are only renamed into place once
complete (and once the checksum matches, if you gave one). Re-running the
same command picks up where it stopped.

Before resuming it checks that the remote file is still the same one —
`ETag`, `Last-Modified` and `Content-Length` are recorded alongside the
partial. If the file changed, it restarts and tells you why rather than
splicing two different files together. A server that ignores `Range` is
detected and handled the same way.

## Backends

`aria2c` when present (real parallel segments), then `curl`, then a pure
Python fallback that always works. Force one with
`--backend aria2c|curl|python`.

## Retries

Timeouts, connection resets, 5xx and 429 are retried with exponential
backoff, honouring `Retry-After`. A 404, 403 or 401 is not retried — it is
not going to get better. `--mirror` sources are tried in order once the
retries for one are exhausted.

Tokens in URLs and `Authorization` headers are redacted in all output.

## Source

[`scripts/dl.py`](https://github.com/HSPK/usm/blob/main/scripts/dl.py)
