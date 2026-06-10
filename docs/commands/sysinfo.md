# `usm sysinfo` & `usm check_py`

Two tiny diagnostic commands.

## `usm sysinfo`

Print a summary of the system, GPU, CUDA, MPI, and distributed-ML
environment. Useful as the first thing to paste when reporting a bug.

```bash
usm sysinfo
```

Sections (depending on what's installed):

- OS / kernel / CPU / RAM
- NVIDIA: `nvidia-smi` parse (driver, CUDA, every GPU)
- CUDA toolkit: `nvcc --version`
- OpenMPI / Intel MPI versions
- NCCL version (from the loaded library if it can find it)
- Python + `torch.cuda` summary if `python -c 'import torch'` succeeds

[`scripts/sysinfo.sh`](https://github.com/HSPK/usm/blob/main/scripts/sysinfo.sh).

## `usm check_py`

Print the active Python 3 and `pip` locations and versions.

```bash
usm check_py
```

Output:

```text
Python3 version: Python 3.10.0
Pip location: /home/user/.pyenv/shims/pip
Pip version: pip 23.0.1 from /home/user/.pyenv/versions/3.10.16/lib/python3.10/site-packages/pip (python 3.10)
```

Handy when something is `pyenv`-shimmed, `uv`-shadowed, or
`conda`-activated and you can't remember which Python is "in front".

[`scripts/check_py.sh`](https://github.com/HSPK/usm/blob/main/scripts/check_py.sh).
