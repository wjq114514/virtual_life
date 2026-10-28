#!/usr/bin/env python3
"""Manage local MoE model server (llama.cpp) + virtual_life API.

Usage:
    ./serve.py start [--model|--api] [--model-only|--api-only] [--yes]
    ./serve.py stop [--model|--api]
    ./serve.py restart [--model|--api]
    ./serve.py status
    ./serve.py logs [--model|--api] [--follow] [-n LINES]
    ./serve.py health
    ./serve.py config
    ./serve.py setup              # Interactive setup wizard

Environment variables (model server):
    MODEL_PATH=/path/to/model.gguf       Required to start model server
    LLAMA_SERVER_BIN=llama-server        Binary (default: llama-server in PATH)
    MODEL_HOST=127.0.0.1                 Bind address (default: 127.0.0.1)
    MODEL_PORT=8092                      Port (default: 8092)
    MODEL_CTX_SIZE=150000                Context tokens (default: 150000)
    MODEL_CACHE_REUSE=65536              KV cache reuse tokens (default: 65536)
    MODEL_THREADS=0                      Threads, 0=auto (default: 0)
    MODEL_PARALLEL=2                     Parallel slots (default: 2)
    MODEL_BATCH_SIZE=512                 Batch size (default: 512)
    MODEL_FLASH_ATTN=false               Flash attention (default: false, P40 不兼容)
    MODEL_MLOCK=true                     Lock memory (default: true)
    MODEL_NO_WEBUI=false                 Disable llama.cpp webui (default: false)
    MODEL_EXTRA_ARGS=                    Extra llama-server args
    MODEL_READY_TIMEOUT=180              Model load timeout seconds

Environment variables (API server):
    VIRTUAL_LIFE_HOST=0.0.0.0            Bind address (default: 0.0.0.0)
    VIRTUAL_LIFE_PORT=8038               Port (default: 8038)
    VIRTUAL_LIFE_READY_TIMEOUT=40        Health check timeout seconds

Data (in .data/, gitignored):
    .data/server-<port>.pid / .data/model-<port>.pid
    .data/server.log / .data/model.log
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / ".data"
APP = "virtual_life.main:app"

# ── ANSI colors (disabled when not a tty) ────────────────────────────────────
_TTY = sys.stdout.isatty()


def _c(text: str, code: str) -> str:
    if not _TTY:
        return text
    return f"\033[{code}m{text}\033[0m"


def green(t: str) -> str:
    return _c(t, "32")


def red(t: str) -> str:
    return _c(t, "31")


def yellow(t: str) -> str:
    return _c(t, "33")


def cyan(t: str) -> str:
    return _c(t, "36")


def dim(t: str) -> str:
    return _c(t, "2")


# ── Model server config ─────────────────────────────────────────────────────
MODEL_HOST = os.environ.get("MODEL_HOST", "127.0.0.1")
MODEL_PORT = int(os.environ.get("MODEL_PORT", "8092"))
MODEL_CTX_SIZE = int(os.environ.get("MODEL_CTX_SIZE", "150000"))
MODEL_CACHE_REUSE = int(os.environ.get("MODEL_CACHE_REUSE", "65536"))
MODEL_THREADS = int(os.environ.get("MODEL_THREADS", "0"))
MODEL_PARALLEL = int(os.environ.get("MODEL_PARALLEL", "2"))
MODEL_BATCH_SIZE = int(os.environ.get("MODEL_BATCH_SIZE", "512"))
MODEL_FLASH_ATTN = os.environ.get("MODEL_FLASH_ATTN", "false").lower() in ("1", "true", "yes")
MODEL_MLOCK = os.environ.get("MODEL_MLOCK", "true").lower() in ("1", "true", "yes")
MODEL_NO_WEBUI = os.environ.get("MODEL_NO_WEBUI", "false").lower() in ("1", "true", "yes")
MODEL_EXTRA_ARGS = os.environ.get("MODEL_EXTRA_ARGS", "").split()
MODEL_READY_TIMEOUT = int(os.environ.get("MODEL_READY_TIMEOUT", "180"))
LLAMA_SERVER_BIN = os.environ.get("LLAMA_SERVER_BIN", "llama-server")

# ── API server config ───────────────────────────────────────────────────────
API_HOST = os.environ.get("VIRTUAL_LIFE_HOST", "0.0.0.0")
API_PORT = int(os.environ.get("VIRTUAL_LIFE_PORT", "8038"))
READY_TIMEOUT = int(os.environ.get("VIRTUAL_LIFE_READY_TIMEOUT", "40"))


# ── GPU detection ───────────────────────────────────────────────────────────
_UNSUPPORTED_FLASH_ATTN_GPUS = ("P40", "P100", "V100", "T4", "M40", "M60")


def _detect_gpu() -> tuple[str | None, str | None]:
    """Return (gpu_name, compute_cap) via nvidia-smi, or (None, None)."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,compute_cap", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            parts = result.stdout.strip().split(",")
            name = parts[0].strip()
            cc = parts[1].strip() if len(parts) > 1 else None
            return name, cc
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return None, None


def _gpu_warnings() -> list[str]:
    """Check GPU and return warnings about known issues."""
    warnings: list[str] = []
    gpu_name, _ = _detect_gpu()
    if not gpu_name:
        return warnings

    for bad_gpu in _UNSUPPORTED_FLASH_ATTN_GPUS:
        if bad_gpu.lower() in gpu_name.lower():
            if MODEL_FLASH_ATTN:
                warnings.append(
                    f"⚠ {gpu_name} 不支持 Flash Attention（旧架构），建议 MODEL_FLASH_ATTN=false"
                )
            break

    return warnings


# ── File helpers ────────────────────────────────────────────────────────────
def _model_pidfile() -> Path:
    return DATA_DIR / f"model-{MODEL_PORT}.pid"


def _api_pidfile() -> Path:
    return DATA_DIR / f"server-{API_PORT}.pid"


def _model_logfile() -> Path:
    return DATA_DIR / "model.log"


def _api_logfile() -> Path:
    return DATA_DIR / "server.log"


def _ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _write_pidfile(path: Path, pid: int, label: str) -> None:
    path.write_text(f"{pid}\n{label}\n")


def _read_pidfile(path: Path, expected_label: str) -> int | None:
    if not path.exists():
        return None
    try:
        lines = path.read_text().strip().split("\n")
        if len(lines) < 2:
            return None
        pid = int(lines[0])
        label = lines[1]
        if label != expected_label:
            return None
        if not Path(f"/proc/{pid}").exists():
            return None
        return pid
    except (ValueError, OSError):
        return None


def _port_busy(host: str, port: int) -> bool:
    import socket

    try:
        with socket.create_connection(
            (host if host != "0.0.0.0" else "127.0.0.1", port), timeout=1
        ):
            return True
    except OSError:
        return False


def _health_check(url: str, timeout: float = 2.0) -> bool:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "virtual-life-serve"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError):
        return False


def _wait_ready(url: str, deadline_seconds: int) -> bool:
    deadline = time.time() + deadline_seconds
    while time.time() < deadline:
        if _health_check(url):
            return True
        time.sleep(0.3)
    return False


def _tail(path: Path, n: int, file=sys.stdout) -> None:
    if not path.exists():
        print(dim(f"日志文件不存在：{path}"), file=file)
        return
    try:
        with path.open() as f:
            lines = f.readlines()[-n:]
            file.write("".join(lines))
    except OSError as e:
        print(f"读取日志失败：{e}", file=file)


# ── API server ──────────────────────────────────────────────────────────────
def _find_uvicon() -> list[str]:
    venv_uv = ROOT / ".venv" / "bin" / "uvicorn"
    if venv_uv.is_file() and os.access(venv_uv, os.X_OK):
        return [str(venv_uv)]
    if shutil.which("uv"):
        return ["uv", "run", "uvicorn"]
    print(red("找不到 uvicorn，也没有 uv。请先运行：uv sync"), file=sys.stderr)
    sys.exit(1)


def _start_api() -> int | None:
    _ensure_data_dir()
    pid_path = _api_pidfile()
    label = f"api:{API_HOST}:{API_PORT}"

    existing = _read_pidfile(pid_path, label)
    if existing:
        print(green(f"✓ API 已在运行（PID {existing}）：http://{API_HOST}:{API_PORT}"))
        return existing

    if _port_busy(API_HOST, API_PORT):
        print(
            red(f"端口 {API_PORT} 已被占用，且不是本脚本启动的进程。"),
            file=sys.stderr,
        )
        print("先处理占用方，或用 VIRTUAL_LIFE_PORT 指定其他端口。", file=sys.stderr)
        return None

    pid_path.unlink(missing_ok=True)
    run_cmd = _find_uvicon() + [
        APP,
        "--host",
        API_HOST,
        "--port",
        str(API_PORT),
        "--no-access-log",
    ]
    log_path = _api_logfile()

    proc = subprocess.Popen(
        run_cmd,
        cwd=str(ROOT),
        stdout=log_path.open("a"),
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    _write_pidfile(pid_path, proc.pid, label)

    health_url = f"http://127.0.0.1:{API_PORT}/healthz"
    print(f"启动 API（PID {proc.pid}）...", end=" ", flush=True)
    if _wait_ready(health_url, READY_TIMEOUT):
        print(green("就绪。"))
        print(f"  WebUI  http://{API_HOST}:{API_PORT}/ui/")
        print(f"  文档   http://{API_HOST}:{API_PORT}/docs")
        print(f"  日志   {log_path}")
        return proc.pid

    print(yellow("超时。最后 20 行日志："), file=sys.stderr)
    _tail(log_path, 20, file=sys.stderr)
    _stop_api()
    return None


def _stop_api() -> bool:
    pid_path = _api_pidfile()
    label = f"api:{API_HOST}:{API_PORT}"
    pid = _read_pidfile(pid_path, label)

    if not pid:
        pid_path.unlink(missing_ok=True)
        print(dim("API 未在运行。"))
        return True

    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass

    deadline = time.time() + 15
    while time.time() < deadline:
        if not _read_pidfile(pid_path, label):
            break
        time.sleep(0.2)

    if _read_pidfile(pid_path, label):
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        time.sleep(0.3)

    pid_path.unlink(missing_ok=True)
    print(green(f"✓ API 已停止（PID {pid}）。"))
    return True


def _status_api() -> int:
    pid_path = _api_pidfile()
    label = f"api:{API_HOST}:{API_PORT}"
    pid = _read_pidfile(pid_path, label)

    if pid:
        print(cyan(f"◆ API 运行中（PID {pid}）：http://{API_HOST}:{API_PORT}"))
        if _health_check(f"http://127.0.0.1:{API_PORT}/healthz"):
            print(green("  ✓ 健康检查通过。"))
        else:
            print(yellow("  ⚠ 进程在，但 /healthz 无响应。"))
        return 0

    pid_path.unlink(missing_ok=True)
    print(dim(f"○ API 未在运行（{API_HOST}:{API_PORT}）。"))
    return 1


# ── Model server ────────────────────────────────────────────────────────────
def _find_llama_server() -> str | None:
    if os.path.isfile(LLAMA_SERVER_BIN) and os.access(LLAMA_SERVER_BIN, os.X_OK):
        return LLAMA_SERVER_BIN
    path = shutil.which(LLAMA_SERVER_BIN)
    return path


def _build_model_cmd() -> list[str] | None:
    model_path = os.environ.get("MODEL_PATH", "")
    if not model_path:
        return None

    binary = _find_llama_server()
    if not binary:
        return None

    if not Path(model_path).exists():
        return None

    ctx = MODEL_CTX_SIZE
    cache = min(MODEL_CACHE_REUSE, ctx)
    parallel = MODEL_PARALLEL
    # llama.cpp divides ctx among parallel slots; ensure each slot ≥ 4096
    per_slot_ctx = ctx // max(1, parallel)
    if per_slot_ctx < 4096:
        parallel = max(1, ctx // 4096)

    cmd: list[str] = [
        binary,
        "-m",
        model_path,
        "--host",
        MODEL_HOST,
        "--port",
        str(MODEL_PORT),
        "--ctx-size",
        str(ctx),
        "--cache-reuse",
        str(cache),
        "--parallel",
        str(parallel),
        "--batch-size",
        str(MODEL_BATCH_SIZE),
        "--threads",
        str(MODEL_THREADS),
    ]
    # --alias 让 /v1/models 返回短名，而不是文件绝对路径；
    # virtual-life.toml [model].name 期望匹配此名。
    alias = os.environ.get("MODEL_ALIAS") or os.path.basename(model_path).removesuffix(".gguf")
    cmd.extend(["--alias", alias])
    if MODEL_FLASH_ATTN:
        cmd.append("--flash-attn")
    if MODEL_MLOCK:
        # llama.cpp ≥ b11xxx: --mlock removed, use --load-mode mlock
        cmd.extend(["--load-mode", "mlock"])
    if MODEL_NO_WEBUI:
        cmd.append("--no-webui")
    cmd.extend(MODEL_EXTRA_ARGS)

    return cmd


def _stop_requested(only_model: bool, only_api: bool) -> None:
    """Stop the subset of services selected by the CLI flags."""
    if only_model:
        _stop_model()
    elif only_api:
        _stop_api()
    else:
        _stop_api()
        _stop_model()


def _start_model() -> int | None:
    _ensure_data_dir()
    pid_path = _model_pidfile()
    label = f"model:{MODEL_HOST}:{MODEL_PORT}"

    existing = _read_pidfile(pid_path, label)
    if existing:
        print(green(f"✓ 模型服务器已在运行（PID {existing}）：http://{MODEL_HOST}:{MODEL_PORT}"))
        return existing

    cmd = _build_model_cmd()
    if cmd is None:
        model_path = os.environ.get("MODEL_PATH", "")
        if not model_path:
            print(red("未设置 MODEL_PATH，无法启动模型服务器。"), file=sys.stderr)
            print("  运行 ./serve.py setup 交互式配置，或：", file=sys.stderr)
            print("  export MODEL_PATH=/path/to/model.gguf", file=sys.stderr)
        else:
            binary = _find_llama_server()
            if not binary:
                print(red(f"找不到 llama-server 二进制：{LLAMA_SERVER_BIN}"), file=sys.stderr)
                print("  设置 LLAMA_SERVER_BIN 指定路径。", file=sys.stderr)
            elif not Path(model_path).exists():
                print(red(f"模型文件不存在：{model_path}"), file=sys.stderr)
        return None

    if _port_busy(MODEL_HOST, MODEL_PORT):
        print(
            red(f"模型端口 {MODEL_PORT} 已被占用，且不是本脚本启动的进程。"),
            file=sys.stderr,
        )
        return None

    # Show config summary + warnings before starting
    model_path = os.environ.get("MODEL_PATH", "")
    ctx = MODEL_CTX_SIZE
    cache = min(MODEL_CACHE_REUSE, ctx)
    parallel = MODEL_PARALLEL
    per_slot_ctx = ctx // max(1, parallel)
    print(cyan("模型服务器配置："))
    print(f"  模型:    {Path(model_path).name}")
    print(f"  地址:    {MODEL_HOST}:{MODEL_PORT}")
    print(f"  上下文:  {ctx:,} tokens ({per_slot_ctx:,}/slot × {parallel} slots)")
    print(f"  缓存:    {cache:,} tokens 复用")
    print(f"  Flash:   {'开启' if MODEL_FLASH_ATTN else '关闭'}")
    print(f"  MLock:   {'开启' if MODEL_MLOCK else '关闭'}")
    for w in _gpu_warnings():
        print(yellow(f"  {w}"))

    pid_path.unlink(missing_ok=True)
    log_path = _model_logfile()

    proc = subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        stdout=log_path.open("a"),
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    _write_pidfile(pid_path, proc.pid, label)

    health_url = f"http://{MODEL_HOST}:{MODEL_PORT}/health"
    print(f"启动模型服务器（PID {proc.pid}）...", end=" ", flush=True)
    if _wait_ready(health_url, MODEL_READY_TIMEOUT):
        print(green("就绪。"))
        print(f"  端点   http://{MODEL_HOST}:{MODEL_PORT}/v1")
        print(f"  日志   {log_path}")
        return proc.pid

    print(yellow(f"超时（{MODEL_READY_TIMEOUT}s）。最后 20 行日志："), file=sys.stderr)
    _tail(log_path, 20, file=sys.stderr)
    _stop_model()
    return None


def _stop_model() -> bool:
    pid_path = _model_pidfile()
    label = f"model:{MODEL_HOST}:{MODEL_PORT}"
    pid = _read_pidfile(pid_path, label)

    if not pid:
        pid_path.unlink(missing_ok=True)
        print(dim("模型服务器未在运行。"))
        return True

    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass

    deadline = time.time() + 30
    while time.time() < deadline:
        if not _read_pidfile(pid_path, label):
            break
        time.sleep(0.5)

    if _read_pidfile(pid_path, label):
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        time.sleep(0.5)

    pid_path.unlink(missing_ok=True)
    print(green(f"✓ 模型服务器已停止（PID {pid}）。"))
    return True


def _status_model() -> int:
    pid_path = _model_pidfile()
    label = f"model:{MODEL_HOST}:{MODEL_PORT}"
    pid = _read_pidfile(pid_path, label)

    if pid:
        print(cyan(f"◆ 模型服务器运行中（PID {pid}）：http://{MODEL_HOST}:{MODEL_PORT}"))
        if _health_check(f"http://{MODEL_HOST}:{MODEL_PORT}/health"):
            print(green("  ✓ 健康检查通过。"))
        else:
            print(yellow("  ⚠ 进程在，但 /health 无响应（可能正在加载模型）。"))
        return 0

    pid_path.unlink(missing_ok=True)
    print(dim(f"○ 模型服务器未在运行（{MODEL_HOST}:{MODEL_PORT}）。"))
    return 1


# ── Config display ──────────────────────────────────────────────────────────
def _print_config() -> None:
    gpu_name, gpu_cc = _detect_gpu()

    print(cyan("═══ 模型服务器 ═══"))
    print(f"  二进制:    {LLAMA_SERVER_BIN}")
    print(f"  模型路径:  {os.environ.get('MODEL_PATH', '(未设置)')}")
    print(f"  地址:      {MODEL_HOST}:{MODEL_PORT}")
    print(f"  上下文:    {MODEL_CTX_SIZE:,} tokens")
    print(f"  缓存复用:  {MODEL_CACHE_REUSE:,} tokens")
    print(f"  并行 slot: {MODEL_PARALLEL}")
    print(f"  批量大小:  {MODEL_BATCH_SIZE}")
    print(f"  线程数:    {MODEL_THREADS} (0=auto)")
    print(f"  Flash Attn: {'开启' if MODEL_FLASH_ATTN else '关闭'}")
    print(f"  MLock:     {'开启' if MODEL_MLOCK else '关闭'}")
    print(f"  WebUI:     {'关闭' if MODEL_NO_WEBUI else '开启'}")
    print(f"  加载超时:  {MODEL_READY_TIMEOUT}s")
    print(f"  额外参数:  {MODEL_EXTRA_ARGS or '(无)'}")
    print()

    print(cyan("═══ API 服务器 ═══"))
    print(f"  地址:      {API_HOST}:{API_PORT}")
    print(f"  就绪超时:  {READY_TIMEOUT}s")
    print()

    print(cyan("═══ GPU 信息 ═══"))
    if gpu_name:
        print(f"  显卡:      {gpu_name}")
        print(f"  计算能力:  {gpu_cc or '(未知)'}")
    else:
        print(dim("  未检测到 NVIDIA GPU（nvidia-smi 不可用）。"))
    print()

    # Known issues
    issues = _gpu_warnings()
    if issues:
        print(yellow("═══ 已知问题 ═══"))
        for issue in issues:
            print(f"  {issue}")
        print()

    print(cyan("═══ MoE 模型建议 ═══"))
    print("  - 150K 上下文适合 MoE 模型（Qwen3-30B-A3B 等）")
    print("  - --cache-reuse 65536 最大化前缀缓存命中")
    print("  - P40 / V100 等旧卡不支持 --flash-attn（已默认关闭）")
    print("  - 并行 slot 自动调整以保证每 slot ≥ 4096 tokens")
    print("  - 完整命令预览：./serve.py config 后对照 MODEL_EXTRA_ARGS")


# ── Interactive setup ───────────────────────────────────────────────────────
def _interactive_setup() -> int:
    """Lightweight setup wizard for MODEL_PATH and optional overrides."""
    print(cyan("═══ virtual-life 服务配置向导 ═══"))
    print()

    # GPU info
    gpu_name, _ = _detect_gpu()
    if gpu_name:
        print(f"检测到 GPU: {green(gpu_name)}")
    else:
        print(dim("未检测到 NVIDIA GPU。"))

    # Model path
    current_path = os.environ.get("MODEL_PATH", "")
    print()
    if current_path:
        exists = Path(current_path).exists()
        status = green("✓") if exists else red("✗")
        print(f"  当前 MODEL_PATH: {status} {current_path}")
    else:
        print(dim("  MODEL_PATH 未设置。"))

    # Find llama-server
    binary = _find_llama_server()
    if binary:
        print(f"  llama-server:    {green('✓')} {binary}")
    else:
        print(f"  llama-server:    {red('✗')} 未找到（{LLAMA_SERVER_BIN}）")

    # Check existing services
    print()
    model_running = _read_pidfile(_model_pidfile(), f"model:{MODEL_HOST}:{MODEL_PORT}")
    api_running = _read_pidfile(_api_pidfile(), f"api:{API_HOST}:{API_PORT}")
    model_health = _health_check(f"http://{MODEL_HOST}:{MODEL_PORT}/health")
    api_health = _health_check(f"http://127.0.0.1:{API_PORT}/healthz")

    print(
        f"  模型服务器:  {'✓ 运行中' if model_running else '○ 未运行'}"
        f"  {'(健康)' if model_health else ''}"
    )
    print(
        f"  API:         {'✓ 运行中' if api_running else '○ 未运行'}"
        f"  {'(健康)' if api_health else ''}"
    )

    # Warnings
    warnings = _gpu_warnings()
    if warnings:
        print()
        for w in warnings:
            print(yellow(f"  {w}"))

    # Next steps
    print()
    print(cyan("═══ 下一步 ═══"))
    if not current_path:
        print("  1. 设置模型路径：")
        print("     export MODEL_PATH=/path/to/your-model.gguf")
        print()
    if not binary:
        print("  2. 安装或指定 llama.cpp：")
        print("     export LLAMA_SERVER_BIN=/path/to/llama-server")
        print()
    print("  3. 启动服务：")
    print("     ./serve.py start          # 模型 + API")
    print("     ./serve.py start --model-only  # 只启动模型")
    print("     ./serve.py start --api-only    # 只启动 API")
    print()
    print("  其他命令：")
    print("     ./serve.py config         # 查看配置")
    print("     ./serve.py status         # 查看状态")
    print("     ./serve.py logs -f        # 跟踪日志")
    print()
    print(dim("完整环境变量列表：./serve.py --help"))

    return 0


# ── Main ────────────────────────────────────────────────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser(
        description="管理本地 MoE 模型服务器 (llama.cpp) + virtual_life API",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "command",
        choices=["start", "stop", "restart", "status", "logs", "config", "health", "setup"],
        nargs="?",
        default="status",
        help="操作命令（默认 status）",
    )
    parser.add_argument(
        "--model",
        "-m",
        action="store_true",
        help="只操作模型服务器（用于 start/stop/status/logs）",
    )
    parser.add_argument(
        "--api",
        "-a",
        action="store_true",
        help="只操作 API 服务器（用于 start/stop/status/logs）",
    )
    parser.add_argument(
        "--model-only",
        action="store_true",
        help="start 时只启动模型服务器，不启动 API",
    )
    parser.add_argument(
        "--api-only",
        action="store_true",
        help="start 时只启动 API，不启动模型服务器",
    )
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="跳过确认提示（用于自动化脚本）",
    )
    parser.add_argument(
        "-n",
        "--lines",
        type=int,
        default=50,
        help="logs 命令显示的行数（默认 50）",
    )
    parser.add_argument(
        "--follow",
        "-f",
        action="store_true",
        help="logs 命令持续跟踪（类似 tail -f）",
    )

    args = parser.parse_args()

    _ensure_data_dir()

    if args.command == "setup":
        return _interactive_setup()

    if args.command == "config":
        _print_config()
        return 0

    if args.command == "health":
        api_ok = _health_check(f"http://127.0.0.1:{API_PORT}/healthz")
        model_ok = _health_check(f"http://{MODEL_HOST}:{MODEL_PORT}/health")
        print(f"API:    {'✓ 正常' if api_ok else '✗ 异常'}")
        print(f"模型:   {'✓ 正常' if model_ok else '✗ 异常'}")
        return 0 if api_ok else 1

    # Determine which services to operate on
    only_model = args.model_only
    only_api = args.api_only
    if args.model and not args.api:
        only_model = True
    elif args.api and not args.model:
        only_api = True

    if args.command == "start":
        exit_code = 0
        if not only_api:
            model_pid = _start_model()
            if model_pid is None:
                exit_code = 1
        if not only_model:
            api_pid = _start_api()
            if api_pid is None:
                exit_code = 1
        return exit_code

    if args.command == "stop":
        _stop_requested(only_model, only_api)
        return 0

    if args.command == "restart":
        _stop_requested(only_model, only_api)
        # Mirror the start branch rather than re-invoking main(), which would
        # re-parse args.command == "restart" and recurse forever.
        exit_code = 0
        if not only_api:
            if _start_model() is None:
                exit_code = 1
        if not only_model:
            if _start_api() is None:
                exit_code = 1
        return exit_code

    if args.command == "status":
        if only_model:
            return _status_model()
        if only_api:
            return _status_api()
        model_code = _status_model()
        api_code = _status_api()
        return max(model_code, api_code)

    if args.command == "logs":
        if args.follow:
            logs = []
            if not only_model:
                logs.append(str(_model_logfile()))
            if not only_api:
                logs.append(str(_api_logfile()))
            if not logs:
                print(dim("无日志文件。"))
                return 1
            cmd = ["tail", "-n", str(args.lines), "-f"] + logs
            try:
                subprocess.run(cmd)
            except KeyboardInterrupt:
                pass
        else:
            if only_api:
                _tail(_api_logfile(), args.lines)
            elif only_model:
                _tail(_model_logfile(), args.lines)
            else:
                print(cyan(f"═══ 模型服务器日志（{MODEL_HOST}:{MODEL_PORT}）═══"))
                _tail(_model_logfile(), args.lines)
                print(cyan(f"\n═══ API 日志（{API_HOST}:{API_PORT}）═══"))
                _tail(_api_logfile(), args.lines)
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
