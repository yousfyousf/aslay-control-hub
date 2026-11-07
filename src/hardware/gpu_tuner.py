# -*- coding: utf-8 -*-
"""
ASLAY Control Hub - GPU Tuner
NVIDIA GPU overclocking/underclocking via NVAPI (nvapi64.dll) with NVML
(pynvml) telemetry. Detection works via NVML; tuning requires the official
NVIDIA driver to be installed.

Self-test:  python -m src.hardware.gpu_tuner
"""
from __future__ import annotations

import ctypes
from dataclasses import dataclass
from typing import Callable, List, Optional

try:
    import pynvml
except ImportError:
    pynvml = None


@dataclass
class GPUDevice:
    index: int
    name: str
    pci_device_id: str
    vram_mb: int
    driver_version: str
    core_clock_mhz: int = 0
    memory_clock_mhz: int = 0
    temperature_c: int = 0
    utilization_percent: int = 0
    power_current_mw: int = 0
    power_min_mw: int = 0
    power_max_mw: int = 0
    core_offset_limit_mhz: int = 0
    memory_offset_limit_mhz: int = 0

    @property
    def power_current_w(self) -> float:
        return round(self.power_current_mw / 1000.0, 1)

    @property
    def power_max_w(self) -> float:
        return round(self.power_max_mw / 1000.0, 1)


class GPUTuner:
    """NVAPI wrapper for core/memory offsets and TGP power limit control."""

    def __init__(self, log: Callable[[str], None] = lambda m: None) -> None:
        self._log = log
        self._nvapi = None
        self._initialized = False
        self._nvml_initialized = False
        # Resolved NVAPI function pointers (resolved once at start)
        self._fn_set_pstates = None

    # ── lifecycle ────────────────────────────────────────────────
    def start(self) -> bool:
        try:
            self._nvapi = ctypes.WinDLL("nvapi64")
        except OSError:
            try:
                self._nvapi = ctypes.WinDLL("nvapi.dll")
            except OSError:
                self._log("[GPU] nvapi.dll not found - install the NVIDIA driver. Tuning disabled.")
                self._nvapi = None
        if self._nvapi is not None:
            try:
                qi = self._nvapi.nvapi_QueryInterface
                qi.restype = ctypes.c_void_p
                init_addr = qi(0x0154E58D)          # NvAPI_Initialize
                if init_addr:
                    init = ctypes.CFUNCTYPE(ctypes.c_int)(init_addr)
                    status = init()
                    if status == 0:
                        self._initialized = True
                        self._log("[GPU] NVAPI initialized.")
                    else:
                        self._log("[GPU] NVAPI init returned status %d" % status)
            except Exception as exc:
                self._log("[GPU] NVAPI init error: %s" % exc)
        if pynvml is not None:
            try:
                pynvml.nvmlInit()
                self._nvml_initialized = True
                self._log("[GPU] NVML (pynvml) initialized.")
            except Exception as exc:
                self._log("[GPU] NVML init failed: %s" % exc)
        return self._initialized or self._nvml_initialized

    def stop(self) -> None:
        if self._nvml_initialized and pynvml is not None:
            try:
                pynvml.nvmlShutdown()
            except Exception:
                pass
        self._initialized = False
        self._nvml_initialized = False
        self._fn_set_pstates = None

    # ── helpers ──────────────────────────────────────────────────
    def _resolve(self, ordinal: int):
        if not self._initialized:
            return None
        qi = self._nvapi.nvapi_QueryInterface
        qi.restype = ctypes.c_void_p
        addr = qi(ordinal)
        return addr

    # ── detection (NVML) ─────────────────────────────────────────
    def detect_physical_gpus(self) -> List[dict]:
        if not self._nvml_initialized:
            return []
        gpus = []
        count = pynvml.nvmlDeviceGetCount()
        for i in range(count):
            h = pynvml.nvmlDeviceGetHandleByIndex(i)
            name = pynvml.nvmlDeviceGetName(h)
            if isinstance(name, bytes):
                name = name.decode("utf-8", "ignore")
            try:
                pci = pynvml.nvmlDeviceGetPciInfo(h)
                pci_id = getattr(pci, "pciDeviceId", "")
            except Exception:
                pci_id = ""
            gpus.append({
                "index": i,
                "name": name,
                "pci_device_id": "%08X" % pci_id if isinstance(pci_id, int) else str(pci_id),
                "vram_mb": pynvml.nvmlDeviceGetMemoryInfo(h).total // (1024 * 1024),
                "driver_version": str(pynvml.nvmlSystemGetDriverVersion()),
            })
        return gpus

    def list_devices(self) -> List[GPUDevice]:
        devices: List[GPUDevice] = []
        if not self._nvml_initialized:
            return devices
        count = pynvml.nvmlDeviceGetCount()
        for i in range(count):
            h = pynvml.nvmlDeviceGetHandleByIndex(i)
            name = pynvml.nvmlDeviceGetName(h)
            if isinstance(name, bytes):
                name = name.decode("utf-8", "ignore")
            mem = pynvml.nvmlDeviceGetMemoryInfo(h)
            util = pynvml.nvmlDeviceGetUtilizationRates(h)
            temp = pynvml.nvmlDeviceGetTemperature(h, pynvml.NVML_TEMPERATURE_GPU)
            power_mw = pynvml.nvmlDeviceGetPowerUsage(h)
            try:
                pmin, pmax = pynvml.nvmlDeviceGetPowerManagementLimitConstraints(h)
            except Exception:
                pmin, pmax = 0, 0
            core = pynvml.nvmlDeviceGetClockInfo(h, pynvml.NVML_CLOCK_GRAPHICS)
            memclk = pynvml.nvmlDeviceGetClockInfo(h, pynvml.NVML_CLOCK_MEM)
            core_limit, mem_limit = self._query_offset_limits(i)
            devices.append(GPUDevice(
                index=i, name=name,
                pci_device_id="", vram_mb=mem.total // (1024 * 1024),
                driver_version=str(pynvml.nvmlSystemGetDriverVersion()),
                core_clock_mhz=core, memory_clock_mhz=memclk,
                temperature_c=temp, utilization_percent=util.gpu,
                power_current_mw=power_mw, power_min_mw=pmin, power_max_mw=pmax,
                core_offset_limit_mhz=core_limit, memory_offset_limit_mhz=mem_limit,
            ))
        return devices

    def _query_offset_limits(self, index: int) -> tuple:
        """Conservative safe offset bounds (MHz) used for clamping."""
        return (300, 800)

    # ── tuning (NVAPI) ──────────────────────────────────────────
    def set_core_offset(self, gpu: GPUDevice, offset_mhz: int) -> bool:
        """
        Apply a core clock offset. Uses NVAPI LKGV (clock offset) which is
        per-domain: 0 = graphics core, 1 = memory. Clamped to safe range.
        Requires Administrator.
        """
        return self._set_clock_offset(gpu.index, domain=0, offset_mhz=offset_mhz,
                                      limit=gpu.core_offset_limit_mhz or 300,
                                      label="Core")

    def set_memory_offset(self, gpu: GPUDevice, offset_mhz: int) -> bool:
        """Apply a memory clock offset (domain 1), clamped to safe range."""
        return self._set_clock_offset(gpu.index, domain=1, offset_mhz=offset_mhz,
                                      limit=gpu.memory_offset_limit_mhz or 800,
                                      label="Memory")

    def _set_clock_offset(self, index: int, domain: int, offset_mhz: int,
                          limit: int, label: str) -> bool:
        if not self._initialized:
            self._log("[GPU] NVAPI unavailable - %s offset not applied." % label)
            return False
        clamped = max(-limit, min(limit, offset_mhz))
        # NvAPI_GPU_SetClientClockOffset is ordinal 0x0F7462BE:
        # (NvPhysicalGpuHandle gpu, void* reserved, int domain, int offsetKHz)
        addr = self._resolve(0x0F7462BE)
        if not addr:
            self._log("[GPU] NVAPI clock-offset function not exported by this driver.")
            return False
        fn = ctypes.CFUNCTYPE(
            ctypes.c_int,                       # status
            ctypes.c_void_p,                    # NvPhysicalGpuHandle
            ctypes.c_void_p,                    # reserved
            ctypes.c_int,                       # clock domain
            ctypes.c_int,                       # offset kHz
        )(addr)
        handle = self._nvml_gpu_handle(index)
        if handle is None:
            self._log("[GPU] Cannot resolve physical GPU handle for index %d." % index)
            return False
        status = fn(handle, None, domain, clamped * 1000)
        if status == 0:
            self._log("[GPU] %s offset set to %+d MHz (clamped within %d)"
                      % (label, clamped, limit))
            return True
        self._log("[GPU] %s offset rejected (status=%d) - driver may lock this." % (label, status))
        return False

    def _nvml_gpu_handle(self, index: int):
        """NVML handles are distinct from NVAPI physical GPU handles, so we
        enumerate NVAPI physical GPUs and map index -> handle by order."""
        if not self._initialized:
            return None
        # NvAPI_EnumPhysicalGPUs ordinal 0xE5AC921F:
        # (NvPhysicalGpuHandle* handles, int* count)
        addr = self._resolve(0xE5AC921F)
        if not addr:
            return None
        fn = ctypes.CFUNCTYPE(
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_int),
        )(addr)
        handles = (ctypes.c_void_p * 64)()
        count = ctypes.c_int(0)
        status = fn(handles, ctypes.byref(count))
        if status != 0 or count.value <= index:
            return None
        return handles[index]

    def set_power_limit(self, gpu: GPUDevice, watts: float) -> bool:
        """Set TGP power limit in watts within driver constraints (NVML)."""
        if not self._nvml_initialized:
            self._log("[GPU] NVML unavailable - power limit not applied.")
            return False
        target_mw = int(watts * 1000)
        if gpu.power_min_mw and target_mw < gpu.power_min_mw:
            target_mw = gpu.power_min_mw
        if gpu.power_max_mw and target_mw > gpu.power_max_mw:
            target_mw = gpu.power_max_mw
        try:
            h = pynvml.nvmlDeviceGetHandleByIndex(gpu.index)
            pynvml.nvmlDeviceSetPowerManagementLimit(h, target_mw)
            self._log("[GPU] Power limit set to %.1f W" % (target_mw / 1000.0))
            return True
        except Exception as exc:
            self._log("[GPU] Power limit set failed: %s" % exc)
            return False

    # ── presets ─────────────────────────────────────────────────
    def enable_game_boost(self, gpu: GPUDevice) -> bool:
        ok1 = self.set_power_limit(gpu, gpu.power_max_w) if gpu.power_max_mw else False
        ok2 = self.set_core_offset(gpu, 90)
        ok3 = self.set_memory_offset(gpu, 400)
        return ok1 or ok2 or ok3

    def reset_offsets(self, gpu: GPUDevice) -> bool:
        ok1 = self.set_core_offset(gpu, 0)
        ok2 = self.set_memory_offset(gpu, 0)
        return ok1 and ok2


def main():
    print("ASLAY GPU Tuner self-test")
    print("=" * 40)
    t = GPUTuner(log=print)
    t.start()
    infos = t.detect_physical_gpus()
    if not infos:
        print("No NVIDIA GPUs via NVML (driver missing or AMD-only machine).")
    for info in infos:
        print("GPU %d: %s, %d MB VRAM, driver %s"
              % (info["index"], info["name"], info["vram_mb"], info["driver_version"]))
    devices = t.list_devices()
    for d in devices:
        print(" - %s: %d MHz core, %d C, %d%% util, %.1f W / max %.1f W"
              % (d.name, d.core_clock_mhz, d.temperature_c, d.utilization_percent,
                 d.power_current_w, d.power_max_w))
        print("   Offset limits: core +/-%d MHz, memory +/-%d MHz"
              % (d.core_offset_limit_mhz, d.memory_offset_limit_mhz))
    t.stop()
    print("Self-test complete. No offsets were applied.")


if __name__ == "__main__":
    main()
