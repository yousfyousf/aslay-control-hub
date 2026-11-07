# -*- coding: utf-8 -*-
"""
ASLAY Control Hub - Process Manager
Live process monitor: PID, CPU %, RAM MB, GPU engine %, Disk I/O, Network
throughput and safe termination (no critical-system-process kills).

Self-test:  python -m src.services.task_manager
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Set

import psutil

CRITICAL_PROCESSES: Set[str] = {
    "system", "system idle process", "registry", "smss.exe", "csrss.exe",
    "wininit.exe", "winlogon.exe", "services.exe", "lsass.exe",
    "svchost.exe", "dwm.exe", "explorer.exe", "fontdrvhost.exe",
    "spoolsv.exe", "memcompression", "conhost.exe",
}

try:
    import pynvml
    _NVML = True
except ImportError:
    _NVML = False


@dataclass
class ProcessInfo:
    pid: int
    name: str
    cpu_percent: float
    ram_mb: float
    gpu_percent: Optional[float] = None
    disk_read_kb_s: float = 0.0
    disk_write_kb_s: float = 0.0
    is_critical: bool = False


@dataclass
class Snapshot:
    timestamp: float
    processes: List[ProcessInfo] = field(default_factory=list)
    total_cpu: float = 0.0
    total_ram_mb: float = 0.0


class ProcessManager:
    """Live process monitoring with safety guards."""

    def __init__(self, log: Callable[[str], None] = lambda m: None) -> None:
        self._log = log
        self._nvml_ok = False
        self._gpu_handles = {}
        if _NVML:
            try:
                pynvml.nvmlInit()
                count = pynvml.nvmlDeviceGetCount()
                for i in range(count):
                    self._gpu_handles[i] = pynvml.nvmlDeviceGetHandleByIndex(i)
                self._nvml_ok = True
                self._log("[PROC] NVML GPU monitoring enabled.")
            except Exception as exc:
                self._log("[PROC] NVML unavailable: %s" % exc)

    def close(self) -> None:
        if self._nvml_ok:
            try:
                pynvml.nvmlShutdown()
            except Exception:
                pass

    def _is_critical(self, name: str) -> bool:
        return name.lower() in CRITICAL_PROCESSES

    def _gpu_usage_for_pid(self, pid: int) -> Optional[float]:
        if not self._nvml_ok:
            return None
        for idx, handle in self._gpu_handles.items():
            try:
                for p in pynvml.nvmlDeviceGetComputeRunningProcesses(handle):
                    if p.pid == pid:
                        try:
                            util = pynvml.nvmlDeviceGetUtilizationRates(handle)
                            return float(util.gpu)
                        except Exception:
                            return None
            except Exception:
                continue
        return None

    def snapshot(self, interval: float = 1.0) -> Snapshot:
        """Take a CPU/RAM/GPU snapshot of all processes."""
        psutil.cpu_percent(interval=None)
        for p in psutil.process_iter(["pid", "name"]):
            try:
                p.cpu_percent(interval=None)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        time.sleep(interval)

        procs: List[ProcessInfo] = []
        for p in psutil.process_iter(["pid", "name", "memory_info"]):
            try:
                cpu = p.cpu_percent(interval=None)
                mem = p.memory_info()
                ram_mb = mem.rss / (1024 * 1024) if mem else 0.0
                name = p.info.get("name") or "Unknown"
                info = ProcessInfo(
                    pid=p.pid,
                    name=name,
                    cpu_percent=round(cpu, 1),
                    ram_mb=round(ram_mb, 1),
                    gpu_percent=self._gpu_usage_for_pid(p.pid),
                    is_critical=self._is_critical(name),
                )
                try:
                    io = p.io_counters()
                    info.disk_read_kb_s = round(io.read_bytes / 1024 / interval, 1)
                    info.disk_write_kb_s = round(io.write_bytes / 1024 / interval, 1)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
                procs.append(info)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        procs.sort(key=lambda x: x.cpu_percent, reverse=True)
        vm = psutil.virtual_memory()
        snap = Snapshot(
            timestamp=time.time(),
            processes=procs,
            total_cpu=psutil.cpu_percent(interval=None),
            total_ram_mb=round(vm.used / (1024 * 1024), 1),
        )
        self._log("[PROC] Snapshot: %d processes, CPU %.1f%%, RAM %.0f MB"
                  % (len(procs), snap.total_cpu, snap.total_ram_mb))
        return snap

    def terminate(self, pid: int, force: bool = False) -> bool:
        """Safely terminate a process. Refuses critical system processes."""
        try:
            p = psutil.Process(pid)
            name = p.name()
            if self._is_critical(name):
                self._log("[PROC] REFUSED: %s (pid %d) is a protected system process."
                          % (name, pid))
                return False
            if force:
                p.kill()
            else:
                p.terminate()
                try:
                    p.wait(timeout=5)
                except psutil.TimeoutExpired:
                    self._log("[PROC] Graceful term timed out - escalating to kill.")
                    p.kill()
            self._log("[PROC] Terminated %s (pid %d)%s"
                      % (name, pid, " [forced]" if force else ""))
            return True
        except psutil.NoSuchProcess:
            self._log("[PROC] pid %d already gone." % pid)
            return True
        except psutil.AccessDenied:
            self._log("[PROC] Access denied terminating pid %d (run as Administrator)." % pid)
            return False


if __name__ == "__main__":
    print("ASLAY Process Manager self-test")
    print("=" * 40)
    pm = ProcessManager(log=print)
    snap = pm.snapshot(interval=1.0)
    print("Total CPU: %.1f%% | Used RAM: %.0f MB" % (snap.total_cpu, snap.total_ram_mb))
    print("Top 10 by CPU:")
    for pr in snap.processes[:10]:
        gpu = ("%.0f%%" % pr.gpu_percent) if pr.gpu_percent is not None else "n/a"
        print("  %-25s pid=%-6d cpu=%5.1f%% ram=%7.1fMB gpu=%s%s"
              % (pr.name[:25], pr.pid, pr.cpu_percent, pr.ram_mb, gpu,
                 " [protected]" if pr.is_critical else ""))
    pm.close()
    print("Self-test complete (no terminations performed).")
