"""Low-overhead host and whole-system electricity telemetry.

Dynamic host values come from procfs.  Whole-system power is read from the
separate root-owned power-meter state/history; this module never invokes IPMI
from an HTTP request.  All methods are bounded and return JSON-safe data.
"""
from __future__ import annotations

import csv
import json
import math
import os
import platform
import re
import socket
import subprocess
import threading
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable


DEFAULT_RATE = 1.10
MAX_HISTORY_POINTS = 144


@dataclass(frozen=True)
class PowerSample:
    epoch: float
    timestamp: str
    watts: float
    integrated: bool


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _iso_now(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="seconds")


def _parse_meminfo(text: str) -> dict[str, int]:
    result: dict[str, int] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        match = re.search(r"([0-9]+)", value)
        if match:
            result[key] = int(match.group(1)) * 1024
    return result


def _parse_cpuinfo(text: str) -> tuple[str, int, int]:
    model = platform.processor() or "x86_64"
    sockets: set[str] = set()
    threads = 0
    for block in text.split("\n\n"):
        fields: dict[str, str] = {}
        for line in block.splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                fields[key.strip()] = value.strip()
        if not fields:
            continue
        if fields.get("model name"):
            model = fields["model name"].replace("(R)", "").replace("(TM)", "")
        if fields.get("physical id") is not None:
            sockets.add(fields["physical id"])
        if fields.get("processor") is not None:
            threads += 1
    socket_count = max(1, len(sockets))
    if socket_count > 1:
        model = f"{socket_count} × {model}"
    return model, socket_count, threads or (os.cpu_count() or 0)


def _parse_dimm_info(text: str) -> tuple[int | None, int]:
    speeds: list[int] = []
    populated = 0
    for block in re.split(r"\n\s*Memory Device\s*\n", text):
        size = re.search(r"^\s*Size:\s*(.+)$", block, re.MULTILINE)
        if not size or "No Module Installed" in size.group(1):
            continue
        populated += 1
        speed = re.search(r"^\s*Configured Memory Speed:\s*([0-9]+)\s*MT/s", block, re.MULTILINE)
        if not speed:
            speed = re.search(r"^\s*Speed:\s*([0-9]+)\s*MT/s", block, re.MULTILINE)
        if speed:
            speeds.append(int(speed.group(1)))
    most_common = Counter(speeds).most_common(1)
    return (most_common[0][0] if most_common else None), populated


def _parse_ticks(text: str) -> tuple[int, int] | None:
    first = text.splitlines()[0].split() if text else []
    if not first or first[0] != "cpu":
        return None
    values = [_safe_int(value) for value in first[1:9]]
    if len(values) < 5:
        return None
    return sum(values), values[3] + values[4]


def _parse_power_sample(row: dict[str, str]) -> PowerSample | None:
    if row.get("status") != "ok":
        return None
    try:
        timestamp = row["timestamp"]
        epoch = datetime.fromisoformat(timestamp).timestamp()
        watts = float(row["watts"])
    except (KeyError, TypeError, ValueError):
        return None
    if not math.isfinite(watts) or not 0 <= watts <= 20_000:
        return None
    return PowerSample(epoch, timestamp, watts, row.get("message") == "integrated by trapezoidal rule")


def integrate_power_window(samples: list[PowerSample], end_epoch: float,
                           window_seconds: float = 86_400) -> tuple[float | None, float, float]:
    start_epoch = end_epoch - window_seconds
    watt_seconds = 0.0
    coverage = 0.0
    for previous, current in zip(samples, samples[1:]):
        if not current.integrated:
            continue
        segment = current.epoch - previous.epoch
        if segment <= 0:
            continue
        left, right = max(previous.epoch, start_epoch), min(current.epoch, end_epoch)
        if right <= left:
            continue
        left_ratio, right_ratio = (left - previous.epoch) / segment, (right - previous.epoch) / segment
        left_watts = previous.watts + (current.watts - previous.watts) * left_ratio
        right_watts = previous.watts + (current.watts - previous.watts) * right_ratio
        clipped = right - left
        watt_seconds += (left_watts + right_watts) * 0.5 * clipped
        coverage += clipped
    if coverage <= 0:
        return None, 0.0, 0.0
    return watt_seconds / coverage, watt_seconds / 3_600_000.0, coverage


class HostMetrics:
    def __init__(self, proc_root: str | Path = "/proc",
                 power_state: str | Path = "/var/lib/power-meter/state.json",
                 power_history: str | Path = "/var/lib/power-meter/history",
                 power_config: str | Path = "/etc/power-meter.conf",
                 clock: Callable[[], float] = time.time):
        self.proc_root = Path(proc_root)
        self.power_state = Path(power_state)
        self.power_history = Path(power_history)
        self.power_config = Path(power_config)
        self.clock = clock
        self.lock = threading.RLock()
        cpu_model, sockets, threads = _parse_cpuinfo(_read_text(self.proc_root / "cpuinfo"))
        memory_speed, dimms = self._memory_modules()
        self.static = {
            "hostname": socket.gethostname(), "cpuModel": cpu_model,
            "cpuSockets": sockets, "cpuThreads": threads,
            "memorySpeedMTs": memory_speed, "memoryModules": dimms,
        }
        self.previous_ticks = _parse_ticks(_read_text(self.proc_root / "stat"))
        self.power_cache_at = 0.0
        self.power_cache: dict[str, Any] | None = None

    def _memory_modules(self) -> tuple[int | None, int]:
        try:
            result = subprocess.run(["dmidecode", "-t", "memory"], check=False,
                                    capture_output=True, text=True, timeout=8,
                                    env={**os.environ, "LC_ALL": "C", "LANG": "C"})
            return _parse_dimm_info(result.stdout) if result.returncode == 0 else (None, 0)
        except (OSError, subprocess.SubprocessError):
            return None, 0

    def _cpu_usage(self) -> float | None:
        current = _parse_ticks(_read_text(self.proc_root / "stat"))
        previous, self.previous_ticks = self.previous_ticks, current
        if not current or not previous:
            return None
        delta_total, delta_idle = current[0] - previous[0], current[1] - previous[1]
        if delta_total <= 0:
            return None
        return round(max(0.0, min(100.0, (delta_total - delta_idle) * 100 / delta_total)), 1)

    def _memory(self) -> tuple[dict[str, Any], dict[str, Any]]:
        data = _parse_meminfo(_read_text(self.proc_root / "meminfo"))
        total, available = data.get("MemTotal", 0), data.get("MemAvailable", 0)
        used = max(0, total - available)
        swap_total, swap_free = data.get("SwapTotal", 0), data.get("SwapFree", 0)
        swap_used = max(0, swap_total - swap_free)
        memory = {"totalBytes": total, "usedBytes": used,
                  "usagePct": round(used * 100 / total, 1) if total else None,
                  "speedMTs": self.static["memorySpeedMTs"], "modules": self.static["memoryModules"]}
        swap = {"totalBytes": swap_total, "usedBytes": swap_used,
                "usagePct": round(swap_used * 100 / swap_total, 1) if swap_total else 0.0}
        return memory, swap

    def _rate(self) -> float:
        rate = DEFAULT_RATE
        for raw in _read_text(self.power_config).splitlines():
            if raw.strip().startswith("RATE_CNY_PER_KWH") and "=" in raw:
                rate = _safe_float(raw.split("=", 1)[1].strip().strip("\"'"), DEFAULT_RATE)
                break
        return rate if 0 <= rate <= 100 else DEFAULT_RATE

    def _samples(self, reference_epoch: float) -> list[PowerSample]:
        reference_date = datetime.fromtimestamp(reference_epoch).astimezone().date()
        samples: list[PowerSample] = []
        for days_ago in range(3):
            path = self.power_history / f"{(reference_date - timedelta(days=days_ago)).isoformat()}.csv"
            try:
                with path.open("r", encoding="utf-8", newline="") as handle:
                    for row in csv.DictReader(handle):
                        parsed = _parse_power_sample(row)
                        if parsed:
                            samples.append(parsed)
            except OSError:
                continue
        cutoff = reference_epoch - 86_700
        unique = {sample.epoch: sample for sample in samples
                  if cutoff <= sample.epoch <= reference_epoch + 5}
        return sorted(unique.values(), key=lambda item: item.epoch)

    @staticmethod
    def _history(samples: list[PowerSample], start_epoch: float) -> list[dict[str, Any]]:
        visible = [sample for sample in samples if sample.epoch >= start_epoch]
        if not visible:
            return []
        chunk_size = max(1, math.ceil(len(visible) / MAX_HISTORY_POINTS))
        result = []
        for offset in range(0, len(visible), chunk_size):
            chunk = visible[offset:offset + chunk_size]
            result.append({"timestamp": chunk[-1].timestamp,
                           "watts": round(sum(item.watts for item in chunk) / len(chunk), 1)})
        return result

    def _power(self, now: float) -> dict[str, Any]:
        if self.power_cache is not None and now - self.power_cache_at < 8:
            return dict(self.power_cache)
        try:
            state = json.loads(self.power_state.read_text(encoding="utf-8"))
            latest_epoch, current_watts = float(state["last_sample_epoch"]), float(state["last_watts"])
            if not math.isfinite(latest_epoch) or not 0 <= current_watts <= 20_000:
                raise ValueError("invalid power state")
            samples = self._samples(latest_epoch)
            average, energy_24h, coverage = integrate_power_window(samples, latest_epoch)
            rate, total_kwh = self._rate(), max(0.0, _safe_float(state.get("total_kwh")))
            age = max(0.0, now - latest_epoch)
            interval = max(1, _safe_int(state.get("sample_interval_seconds"), 60))
            fresh = age <= max(35, interval * 3)
            forecast_30d = average * 24 * 30 / 1000 * rate if average is not None else None
            value = {
                "status": "live" if fresh else "stale",
                "source": str(state.get("last_source") or "ipmi-dcmi"),
                "currentW": round(current_watts, 1),
                "average24hW": round(average, 1) if average is not None else None,
                "coverageSeconds24h": round(coverage, 1),
                "rateCnyPerKwh": round(rate, 4),
                "totalKwh": round(total_kwh, 6),
                "totalCostCny": round(total_kwh * rate, 4),
                "cost24hCny": round(energy_24h * rate, 4),
                "forecast30dCny": round(forecast_30d, 2) if forecast_30d is not None else None,
                "firstSampleAt": state.get("first_sample_at"),
                "lastSampleAt": state.get("last_sample_at"),
                "sampleAgeSeconds": round(age, 1),
                "samplesOk": _safe_int(state.get("samples_ok")),
                "samplesFailed": _safe_int(state.get("samples_failed")),
                "gaps": _safe_int(state.get("gaps")),
                "history24h": self._history(samples, latest_epoch - 86_400),
            }
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            value = {
                "status": "collecting", "source": "ipmi-dcmi", "currentW": None,
                "average24hW": None, "coverageSeconds24h": 0.0,
                "rateCnyPerKwh": round(self._rate(), 4), "totalKwh": 0.0,
                "totalCostCny": 0.0, "cost24hCny": 0.0, "forecast30dCny": None,
                "firstSampleAt": None, "lastSampleAt": None, "sampleAgeSeconds": None,
                "samplesOk": 0, "samplesFailed": 0, "gaps": 0, "history24h": [],
            }
        self.power_cache_at, self.power_cache = now, value
        return dict(value)

    def power_snapshot(self) -> dict[str, Any]:
        """Return only meter data, including when the monitored node is off."""
        with self.lock:
            return self._power(self.clock())

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            now = self.clock()
            memory, swap = self._memory()
            return {
                "status": "live" if memory["totalBytes"] else "partial",
                "observedAt": _iso_now(now),
                "cpu": {"model": self.static["cpuModel"], "sockets": self.static["cpuSockets"],
                        "threads": self.static["cpuThreads"], "usagePct": self._cpu_usage()},
                "memory": memory, "swap": swap, "power": self._power(now),
            }
