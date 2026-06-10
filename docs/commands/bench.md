# `usm bench`

Quick health-check benchmark for a fresh VM or container. **Not a serious
benchmarking suite** — this is a 30-second sanity test to catch "spot VM
claims 64 cores but has IOPS like a USB stick" cases.

```bash
usm bench                     # quick mode (~30s)
usm bench --full              # bigger sizes, more samples
usm bench --no-net --no-gpu   # offline / non-GPU machines
```

## What it measures

| Section | What |
| --- | --- |
| **CPU** | Single-thread float Mops/s (tight inner loop in Python); core counts; load averages |
| **Memory** | Total / available / used; `bytearray` copy throughput for 256 MiB (quick) / 1 GiB (full) |
| **Disk** | Sequential write + read of 256 MiB (quick) / 1 GiB (full) to `$TMPDIR`. `fsync` before measuring write throughput. |
| **Network** | `ping 1.1.1.1` min/avg/max; HTTPS download of a 10 MB Cloudflare speed test |
| **GPU** | If `torch` + CUDA available: `matmul` TFLOPS for fp32 and fp16 on a 4096² (quick) or 8192² (full) matrix |

## Skip flags

`--no-cpu`, `--no-mem`, `--no-disk`, `--no-net`, `--no-gpu` for any subset.

## Honesty notes

- The CPU number is **Python op rate**, not native FLOPS. Comparable across
  machines for the same Python version; not comparable to other tools.
- Disk throughput depends heavily on `$TMPDIR` (tmpfs vs SSD vs spinning
  rust). Don't compare numbers across machines unless their `$TMPDIR`
  backends match.
- Network test goes to the public Internet — your VM's egress shaping,
  not local NIC speed.
- GPU FLOPS via `torch.matmul` is roughly representative of dense training
  workloads, not arbitrary kernels.

For real benchmarking: `fio`, `iperf3`, `sysbench`, `mlperf`, `cublas-bench`.

## Source

[`scripts/bench.py`](https://github.com/HSPK/usm/blob/main/scripts/bench.py)
