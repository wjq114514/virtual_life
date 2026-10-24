#!/usr/bin/env bash
# 启停 virtual_life API（后台常驻）。
#
#   ./serve.sh start|stop|restart|status|logs
#
# 环境变量：VIRTUAL_LIFE_HOST（默认 0.0.0.0）、VIRTUAL_LIFE_PORT（默认 8038）。
# PID 与日志写入已被 .gitignore 排除的 .data/。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

HOST="${VIRTUAL_LIFE_HOST:-0.0.0.0}"
PORT="${VIRTUAL_LIFE_PORT:-8038}"
APP="virtual_life.main:app"
DATA_DIR="$ROOT/.data"
# 按端口分文件，这样同时跑多个实例时不会互相覆盖记录
PID_FILE="$DATA_DIR/server-$PORT.pid"
LOG_FILE="$DATA_DIR/server.log"
READY_TIMEOUT="${VIRTUAL_LIFE_READY_TIMEOUT:-40}"

mkdir -p "$DATA_DIR"

# 解析出真正可执行的 uvicorn：优先用已同步的 venv，其次退回 uv run。
if [[ -x "$ROOT/.venv/bin/uvicorn" ]]; then
    RUN=(.venv/bin/uvicorn "$APP" --host "$HOST" --port "$PORT" --no-access-log)
elif command -v uv >/dev/null 2>&1; then
    RUN=(uv run uvicorn "$APP" --host "$HOST" --port "$PORT" --no-access-log)
else
    echo "找不到 uvicorn，也没有 uv。请先运行：uv sync" >&2
    exit 1
fi

# PID 文件两行：pid、host:port。第二行用来确认这个实例确实监听在请求的地址上，
# 否则换个 host 调用会误判成“已在运行”。取 pid 用 `head -1`。
write_pidfile() {
    printf '%s\n%s:%s\n' "$1" "$HOST" "$PORT" > "$PID_FILE"
}

running_pid() {
    [[ -f "$PID_FILE" ]] || return 1
    local pid bound
    pid="$(head -n1 "$PID_FILE" 2>/dev/null || true)"
    bound="$(sed -n '2p' "$PID_FILE" 2>/dev/null || true)"
    [[ -n "$pid" && -n "$bound" ]] || return 1
    [[ "$bound" == "$HOST:$PORT" ]] || return 1
    [[ -d "/proc/$pid" ]] || return 1
    # 确认这个 PID 确实是我们启动的服务，避免误杀复用的 PID
    tr '\0' ' ' < "/proc/$pid/cmdline" | grep -q "virtual_life.main:app" || return 1
    printf '%s' "$pid"
}

# 列出本脚本记录过的所有实例，便于 stop/status 之外的排查
tracked_instances() {
    shopt -s nullglob
    for file in "$DATA_DIR"/server-*.pid; do
        printf '  %s (pid %s)\n' "$(sed -n '2p' "$file")" "$(head -n1 "$file")"
    done
    shopt -u nullglob
}

port_busy() {
    # 不依赖 ss/netstat/lsof：直接试连端口
    (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null && exec 3<&- && return 0
    return 1
}

wait_ready() {
    local deadline=$((SECONDS + READY_TIMEOUT))
    while ((SECONDS < deadline)); do
        if curl -fsS --max-time 2 "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then
            return 0
        fi
        sleep 0.3
    done
    return 1
}

cmd_start() {
    if pid="$(running_pid)"; then
        echo "已在运行（PID $pid）：http://$HOST:$PORT"
        return 0
    fi
    rm -f "$PID_FILE"
    if port_busy; then
        echo "端口 $PORT 已被占用，且不是本脚本启动的进程。" >&2
        echo "先处理占用方，或用 VIRTUAL_LIFE_PORT 指定其他端口。" >&2
        return 1
    fi

    # setsid 让服务脱离当前终端，这样关掉终端或父 shell 被杀都不会带走它
    setsid nohup "${RUN[@]}" >>"$LOG_FILE" 2>&1 </dev/null &
    local pid=$!
    write_pidfile "$pid"

    if wait_ready; then
        echo "已启动（PID $pid）：http://$HOST:$PORT"
        echo "  WebUI  http://$HOST:$PORT/ui/"
        echo "  文档   http://$HOST:$PORT/docs"
        echo "  日志   $LOG_FILE"
    else
        echo "启动失败或 $READY_TIMEOUT 秒内未就绪。最后 20 行日志：" >&2
        tail -n 20 "$LOG_FILE" >&2
        cmd_stop >/dev/null 2>&1 || true
        return 1
    fi
}

cmd_stop() {
    local pid
    if ! pid="$(running_pid)"; then
        rm -f "$PID_FILE"
        echo "未在运行。"
        return 0
    fi
    # 杀整个进程组：uv run 会再 fork 出一个 uvicorn，只杀父进程会留下孤儿
    kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
    local deadline=$((SECONDS + 15))
    while ((SECONDS < deadline)); do
        running_pid >/dev/null || break
        sleep 0.2
    done
    if running_pid >/dev/null; then
        kill -KILL -- "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
        sleep 0.3
    fi
    rm -f "$PID_FILE"
    echo "已停止（PID $pid）。"
}

cmd_status() {
    local pid
    if pid="$(running_pid)"; then
        echo "运行中（PID $pid）：http://$HOST:$PORT"
        if curl -fsS --max-time 2 "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then
            echo "健康检查通过。"
        else
            echo "警告：进程在，但 /healthz 无响应。"
        fi
        return 0
    fi
    rm -f "$PID_FILE"
    echo "未在运行（$HOST:$PORT）。"
    local others
    others="$(tracked_instances)"
    [[ -n "$others" ]] && { echo "本脚本记录的其他实例："; echo "$others"; }
    return 1
}

case "${1:-status}" in
    start) cmd_start ;;
    stop) cmd_stop ;;
    restart) cmd_stop; cmd_start ;;
    status) cmd_status ;;
    logs) tail -f "$LOG_FILE" ;;
    *)
        echo "用法：$0 {start|stop|restart|status|logs}" >&2
        exit 2
        ;;
esac