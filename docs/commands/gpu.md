# `usm gpu`

GPU inventory, free-picker, watch, and kill on top of `nvidia-smi`.

```bash
usm gpu                 # summary table
usm gpu free            # index of the least-loaded GPU
usm gpu free 2          # CUDA_VISIBLE_DEVICES=$(usm gpu free 2)
usm gpu watch           # live-refreshing table
usm gpu kill 12345      # SIGTERM a CUDA process (pid)
usm gpu kill --user alice
usm gpu kill 12345 --force   # SIGKILL immediately
```

## What you get

- **`(default)`** — one-shot table: idx / name / util / memory / temp / power /
  per-GPU process list (user + pid + per-proc memory). Util and memory cells
  are color-coded (green < 30%/50%, yellow, red ≥ 80%).
- **`free [N]`** — prints the index(es) of the `N` least-loaded GPUs as a
  comma-separated string, suitable for
  `CUDA_VISIBLE_DEVICES=$(usm gpu free 2) python train.py`. By default a
  GPU counts as "free" when `mem_used < 500 MiB` and `util < 10%`; if not
  enough satisfy that, the next-loaded ones fill the gap.
- **`watch [-n INTERVAL]`** — `rich.live` refresh every `INTERVAL` seconds
  (default 1.0). Ctrl-C to exit.
- **`kill <pid|user>` / `--user NAME` / `--force`** — terminates CUDA
  processes. By default SIGTERM; `--force` sends SIGKILL.

## Requirements

- `nvidia-smi` on PATH (i.e. NVIDIA driver installed).
- No need for `pynvml` / NVML bindings — everything is parsed from
  `nvidia-smi --query-* --format=csv`.

## Source

[`scripts/gpu.py`](https://github.com/HSPK/usm/blob/main/scripts/gpu.py)
