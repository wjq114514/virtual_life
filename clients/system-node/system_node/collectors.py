"""Sample only the running environment and explicitly configured endpoints."""

import math
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from .config import Node
from .transport import probe_http, probe_tcp

METRIC_NAMES = (
    "cpu_usage_percent",
    "memory_total_bytes",
    "memory_available_bytes",
    "memory_used_percent",
    "uptime_seconds",
    "load_average",
    "disk_total_bytes",
    "disk_free_bytes",
    "disk_used_percent",
)


def _proc_text(name: str, limit: int = 65_536) -> str:
    with Path("/proc", name).open("r", encoding="ascii") as stream:
        value = stream.read(limit + 1)
    if len(value) > limit:
        raise ValueError("proc_too_large")
    return value


class LocalCollector:
    def __init__(self, disk_path: Path):
        self._disk_path = disk_path
        self._previous_cpu: tuple[int, int] | None = None

    def collect(self) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        metrics: dict[str, Any] = dict.fromkeys(METRIC_NAMES)
        errors: list[str] = []
        if sys.platform == "linux":
            self._cpu(metrics, errors)
            self._memory(metrics, errors)
            self._uptime(metrics, errors)
            self._load(metrics, errors)
            adapter = "linux_proc"
        else:
            errors.extend(("cpu_unsupported", "memory_unsupported", "uptime_unsupported"))
            self._load(metrics, errors)
            adapter = "platform"
        self._disk(metrics, errors)
        available = any(value is not None for value in metrics.values())
        quality = "unknown" if not available else "partial" if errors else "ok"
        source = {
            "adapter": adapter,
            "scope": "runtime_environment",
            "quality": quality,
            "errors": errors,
        }
        reachability = {
            "status": "reachable" if available else "unknown",
            "latency_ms": None,
            "http_status": None,
        }
        return source, reachability, metrics

    def _cpu(self, metrics: dict[str, Any], errors: list[str]) -> None:
        try:
            # Only the aggregate row is needed, including on machines with many cores.
            with Path("/proc/stat").open("r", encoding="ascii") as stream:
                line = stream.readline(8193)
            parts = line.split()
            if len(line) > 8192 or len(parts) < 5 or parts[0] != "cpu":
                raise ValueError("cpu_invalid")
            values = [int(value) for value in parts[1:9]]
            if any(value < 0 for value in values):
                raise ValueError("cpu_invalid")
            # guest and guest_nice are already counted in user and nice.
            total = sum(values)
            idle = values[3] + (values[4] if len(values) > 4 else 0)
            previous = self._previous_cpu
            self._previous_cpu = (total, idle)
            if previous is None:
                errors.append("cpu_warmup")
                return
            total_delta, idle_delta = total - previous[0], idle - previous[1]
            if total_delta <= 0 or not 0 <= idle_delta <= total_delta:
                errors.append("cpu_delta_invalid")
                return
            metrics["cpu_usage_percent"] = round(100 * (total_delta - idle_delta) / total_delta, 3)
        except (OSError, ValueError, UnicodeError, OverflowError):
            self._previous_cpu = None
            errors.append("cpu_unavailable")

    def _memory(self, metrics: dict[str, Any], errors: list[str]) -> None:
        try:
            values = {}
            for line in _proc_text("meminfo").splitlines():
                key, _, value = line.partition(":")
                if key in {"MemTotal", "MemAvailable"}:
                    parts = value.split()
                    if len(parts) != 2 or parts[1] != "kB":
                        raise ValueError("memory_invalid")
                    values[key] = int(parts[0]) * 1024
            total = values.get("MemTotal")
            available = values.get("MemAvailable")
            if total is None or total <= 0:
                errors.append("memory_total_unavailable")
                return
            metrics["memory_total_bytes"] = total
            if available is None or not 0 <= available <= total:
                errors.append("memory_available_unavailable")
                return
            metrics["memory_available_bytes"] = available
            metrics["memory_used_percent"] = round(100 * (total - available) / total, 3)
        except (OSError, ValueError, UnicodeError, OverflowError):
            errors.append("memory_unavailable")

    def _uptime(self, metrics: dict[str, Any], errors: list[str]) -> None:
        try:
            value = float(_proc_text("uptime", 4096).split()[0])
            if not math.isfinite(value) or value < 0:
                raise ValueError("uptime_invalid")
            metrics["uptime_seconds"] = value
        except (OSError, ValueError, IndexError, UnicodeError, OverflowError):
            errors.append("uptime_unavailable")

    def _load(self, metrics: dict[str, Any], errors: list[str]) -> None:
        try:
            if sys.platform == "linux":
                values = [float(value) for value in _proc_text("loadavg", 4096).split()[:3]]
            else:
                values = list(os.getloadavg())
            if len(values) != 3 or any(not math.isfinite(value) or value < 0 for value in values):
                raise ValueError("load_invalid")
            metrics["load_average"] = values
        except (OSError, ValueError, AttributeError, UnicodeError, OverflowError):
            errors.append("load_unavailable")

    def _disk(self, metrics: dict[str, Any], errors: list[str]) -> None:
        try:
            usage = shutil.disk_usage(self._disk_path)
            if (
                usage.total <= 0
                or not 0 <= usage.free <= usage.total
                or not 0 <= usage.used <= usage.total
            ):
                raise ValueError("disk_invalid")
            metrics["disk_total_bytes"] = usage.total
            metrics["disk_free_bytes"] = usage.free
            metrics["disk_used_percent"] = round(100 * usage.used / usage.total, 3)
        except (OSError, ValueError, OverflowError):
            errors.append("disk_unavailable")


def collect_target(node: Node) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    if node.adapter == "tcp":
        result = probe_tcp(node.host, node.port, node.timeout_seconds)
    else:
        result = probe_http(node.url, node.timeout_seconds)
    source = {
        "adapter": node.adapter,
        "scope": "service_endpoint",
        "quality": result.quality,
        "errors": list(result.errors),
    }
    reachability = {
        "status": result.status,
        "latency_ms": result.latency_ms,
        "http_status": result.http_status,
    }
    return source, reachability, {}
