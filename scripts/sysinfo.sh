#!/usr/bin/env bash
# sysinfo.sh – Print system, GPU, and distributed-ML environment summary
set -euo pipefail

BOLD='\033[1m'
CYAN='\033[1;36m'
GREEN='\033[1;32m'
YELLOW='\033[1;33m'
DIM='\033[2m'
RESET='\033[0m'

section() { printf "\n${CYAN}━━━ %s ━━━${RESET}\n" "$1"; }
kv()      { printf "  ${BOLD}%-24s${RESET} %s\n" "$1" "$2"; }
kv_env()  {
    local val="${!1:-}"
    if [ -n "$val" ]; then
        kv "$1" "$val"
    else
        printf "  ${BOLD}%-24s${RESET} ${DIM}(not set)${RESET}\n" "$1"
    fi
}

# ── OS & Kernel ──────────────────────────────────────────
section "OS & Kernel"
kv "Hostname" "$(hostname)"
kv "OS" "$(. /etc/os-release 2>/dev/null && echo "$PRETTY_NAME" || uname -s)"
kv "Kernel" "$(uname -r)"
kv "Arch" "$(uname -m)"
kv "Uptime" "$(uptime -p 2>/dev/null || uptime | sed 's/.*up /up /' | sed 's/,.*//')"

# ── CPU & Memory ─────────────────────────────────────────
section "CPU & Memory"
kv "CPU Model" "$(lscpu 2>/dev/null | awk -F: '/Model name/{gsub(/^ +/,"",$2); print $2; exit}')"
kv "CPU Cores" "$(nproc 2>/dev/null || echo unknown)"
kv "Memory" "$(free -h 2>/dev/null | awk '/Mem:/{printf "%s used / %s total", $3, $2}')"
kv "Swap" "$(free -h 2>/dev/null | awk '/Swap:/{printf "%s used / %s total", $3, $2}')"

# ── Disk ─────────────────────────────────────────────────
section "Disk"
df -h / --output=size,used,avail,pcent 2>/dev/null | head -2 | tail -1 | \
    awk '{printf "  %-24s %s used / %s total (%s free, %s used)\n", "/", $2, $1, $3, $4}'

# ── Network ──────────────────────────────────────────────
section "Network"
kv "Hostname -I" "$(hostname -I 2>/dev/null | awk '{print $1}')"
if command -v ip &>/dev/null; then
    kv "Default Iface" "$(ip route show default 2>/dev/null | awk '{print $5; exit}')"
fi
if command -v ibstat &>/dev/null; then
    ib_state=$(ibstat 2>/dev/null | awk '/State:/{print $2; exit}')
    ib_rate=$(ibstat 2>/dev/null | awk '/Rate:/{print $2; exit}')
    kv "InfiniBand" "${ib_state:-N/A} ${ib_rate:+(${ib_rate} Gb/s)}"
elif command -v ibv_devinfo &>/dev/null; then
    kv "InfiniBand" "$(ibv_devinfo 2>/dev/null | awk '/hca_id/{print $2; exit}' || echo 'N/A')"
else
    kv "InfiniBand" "(ibstat not found)"
fi

# ── GPU (NVIDIA) ─────────────────────────────────────────
section "GPU"
if command -v nvidia-smi &>/dev/null; then
    kv "Driver" "$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -1)"
    kv "CUDA (driver)" "$(nvidia-smi 2>/dev/null | grep -oP 'CUDA Version: \K[0-9.]+')"
    gpu_count=$(nvidia-smi --query-gpu=name --format=csv,noheader | wc -l)
    kv "GPU Count" "$gpu_count"
    echo ""
    nvidia-smi --query-gpu=index,name,memory.total,memory.used,utilization.gpu,temperature.gpu \
        --format=csv,noheader | while IFS=, read -r idx name mem_total mem_used util temp; do
        printf "  ${GREEN}[%s]${RESET} %-30s %s / %s  util: %s  temp: %s\n" \
            "$(echo "$idx" | xargs)" \
            "$(echo "$name" | xargs)" \
            "$(echo "$mem_used" | xargs)" \
            "$(echo "$mem_total" | xargs)" \
            "$(echo "$util" | xargs)" \
            "$(echo "$temp" | xargs)°C"
    done
else
    kv "NVIDIA GPU" "(nvidia-smi not found)"
fi

# ── CUDA Toolkit ─────────────────────────────────────────
section "CUDA Toolkit"
if command -v nvcc &>/dev/null; then
    kv "nvcc" "$(nvcc --version 2>/dev/null | grep -oP 'release \K[0-9.]+')"
    kv "nvcc path" "$(command -v nvcc)"
else
    kv "nvcc" "(not found)"
fi
kv_env CUDA_HOME
kv_env CUDA_PATH
kv_env LD_LIBRARY_PATH

# ── Python & ML Frameworks ───────────────────────────────
section "Python & ML"
if command -v python3 &>/dev/null; then
    kv "Python" "$(python3 --version 2>&1) ($(command -v python3))"
else
    kv "Python" "(not found)"
fi
if python3 -c "import torch" 2>/dev/null; then
    kv "PyTorch" "$(python3 -c 'import torch; print(torch.__version__)')"
    kv "Torch CUDA" "$(python3 -c 'import torch; print(torch.version.cuda or "N/A")')"
    kv "Torch cuDNN" "$(python3 -c 'import torch; print(torch.backends.cudnn.version() if torch.backends.cudnn.is_available() else "N/A")')"
    kv "Torch GPUs" "$(python3 -c 'import torch; print(torch.cuda.device_count())')"
    kv "NCCL version" "$(python3 -c 'import torch; print(torch.cuda.nccl.version() if torch.cuda.is_available() else "N/A")' 2>/dev/null || echo 'N/A')"
fi
if python3 -c "import deepspeed" 2>/dev/null; then
    kv "DeepSpeed" "$(python3 -c 'import deepspeed; print(deepspeed.__version__)')"
fi

# ── MPI ──────────────────────────────────────────────────
section "MPI"
if command -v mpirun &>/dev/null; then
    kv "mpirun" "$(mpirun --version 2>&1 | head -1)"
    kv "mpirun path" "$(command -v mpirun)"
else
    kv "mpirun" "(not found)"
fi
kv_env OMPI_COMM_WORLD_SIZE
kv_env OMPI_COMM_WORLD_RANK
kv_env OMPI_COMM_WORLD_LOCAL_RANK
kv_env MPI_LOCALRANKID
kv_env MPI_LOCALNRANKS

# ── Distributed ML Environment Variables ─────────────────
section "Distributed / Multi-GPU Env Vars"

printf "  ${YELLOW}── torchrun / torch.distributed ──${RESET}\n"
kv_env MASTER_ADDR
kv_env MASTER_PORT
kv_env WORLD_SIZE
kv_env RANK
kv_env LOCAL_RANK
kv_env LOCAL_WORLD_SIZE
kv_env GROUP_RANK
kv_env ROLE_RANK
kv_env TORCHELASTIC_RESTART_COUNT
kv_env TORCHELASTIC_MAX_RESTARTS
kv_env OMP_NUM_THREADS

printf "\n  ${YELLOW}── NCCL ──${RESET}\n"
kv_env NCCL_DEBUG
kv_env NCCL_SOCKET_IFNAME
kv_env NCCL_IB_DISABLE
kv_env NCCL_IB_HCA
kv_env NCCL_NET_GDR_LEVEL
kv_env NCCL_P2P_DISABLE
kv_env NCCL_SHM_DISABLE
kv_env NCCL_ALGO
kv_env NCCL_PROTO

printf "\n  ${YELLOW}── DeepSpeed / Accelerate ──${RESET}\n"
kv_env DEEPSPEED_CONFIG
kv_env ACCELERATE_CONFIG_FILE
kv_env HF_HOME

printf "\n  ${YELLOW}── Azure ML / Singularity ──${RESET}\n"
kv_env AZ_BATCH_NODE_LIST
kv_env DLTS_JOB_ID
kv_env DLWS_NUM_GPU_PER_WORKER
kv_env NODE_RANK

printf "\n  ${YELLOW}── Visibility ──${RESET}\n"
kv_env CUDA_VISIBLE_DEVICES
kv_env NVIDIA_VISIBLE_DEVICES
kv_env GPU_DEVICE_ORDINAL

echo ""
