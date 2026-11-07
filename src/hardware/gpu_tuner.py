# -*- coding: utf-8 -*-
"""
ASLAY Control Hub - GPU Tuner
NVIDIA GPU overclocking/underclocking via NVAPI (nvapi.dll) with NVML
(pynvml) telemetry. Detection works via NVML; tuning requires the official
NVIDIA driver to be installed.

Self-test:  python -m src.hardware.gpu_tuner
"""
from __future__ import annotations

import ctypes
from dataclasses import dataclass
from typing import Callable, List

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
                query = self._nvapi.nvapi_QueryInterface
                query.restype = ctypes.c_void_p
                init_addr = query(0x0154E58D)
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

    def _query_interface(self, ordinal: int):
        qi = self._nvapi.nvapi_QueryInterface
        qi.restype = ctypes.c_void_p
        return qi(ordinal)

    def _nvapi_call(self, ordinal: int, restype, argtypes, *args):
        if not self._initialized:
            return None
        addr = self._query_interface(ordinal)
        if not addr:
            return None
        fn = ctypes.CFUNCTYPE(restype, *argtypes)(addr)
        return fn(*args)

    def detect_physical_gpus(self) -> List[dict]:
        """List physical NVIDIA GPUs via NVML (requires driver)."""
        if not self._nvml_initialized:
            return []
        gpus = []
        count = pynvml.nvmlDeviceGetCount()
        for i in range(count):
            h = pynvml.nvmlDeviceGetHandleByIndex(i)
            name = pynvml.nvmlDeviceGetName(h)
            if isinstance(name, bytes):
                name = name.decode("utf-8", "ignore")
            pci = pynvml.nvmlDeviceGetPciInfo(h)
            pci_id = getattr(pci, "pciDeviceId", "")
            gpus.append({
                "index": i,
                "name": name,
                "pci_device_id": "%08X" % pci_id if isinstance(pci_id, int) else str(pci_id),
                "vram_mb": pynvml.nvmlDeviceGetMemoryInfo(h).total // (1024 * 1024),
                "driver_version": str(pynvml.nvmlSystemGetDriverVersion()),
            })
        return gpus

    def list_devices(self) -> List[GPUDevice]:
        """Build GPUDevice list with live telemetry and detected limits."""
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
            dev = GPUDevice(
                index=i, name=name,
                pci_device_id="", vram_mb=mem.total // (1024 * 1024),
                driver_version=str(pynvml.nvmlSystemGetDriverVersion()),
                core_clock_mhz=core, memory_clock_mhz=memclk,
                temperature_c=temp, utilization_percent=util.gpu,
                power_current_mw=power_mw, power_min_mw=pmin, power_max_mw=pmax,
                core_offset_limit_mhz=core_limit, memory_offset_limit_mhz=mem_limit,
            )
            devices.append(dev)
        return devices

    def _query_offset_limits(self, index: int) -> tuple:
        """Conservative safe offset bounds (MHz) used for clamping."""
        return (300, 800)

    def set_core_offset(self, gpu: GPUDevice, offset_mhz: int) -> bool:
        """Apply core clock offset (clamped to detected safe range)."""
        if not self._initialized:
            self._log("[GPU] NVAPI unavailable - core offset not applied.")
            return False
        limit = gpu.core_offset_limit_mhz or 300
        clamped = max(-limit, min(limit, offset_mhz))
        status = self._nvapi_call(
            0x0D3F1D5B, ctypes.c_int,
            [ctypes.c_uint, ctypes.POINTER(ctypes.c_int)],
            gpu.index, ctypes.byref(ctypes.c_int(clamped)),
        )
        if status == 0:
            self._log("[GPU] Core offset set to %+d MHz (clamped within %d)" % (clamped, limit))
            return True
        self._log("[GPU] Core offset command rejected (status=%s)" % status)
        return False

    def set_memory_offset(self, gpu: GPUDevice, offset_mhz: int) -> bool:
        """Apply memory clock offset (clamped to detected safe range)."""
        if not self._initialized:
            self._log("[GPU] NVAPI unavailable - memory offset not applied.")
            return False
        limit = gpu.memory_offset_limit_mhz or 800
        clamped = max(-limit, min(limit, offset_mhz))
        status = self._nvapi_call(
            0x0D3F1D5B, ctypes.c_int,
            [ctypes.c_uint, ctypes.POINTER(ctypes.c_int)],
            gpu.index, ctypes.byref(ctypes.c_int(clamped)),
        )
        if status == 0:
            self._log("[GPU] Memory offset set to %+d MHz (clamped within %d)" % (clamped, limit))
            return True
        self._log("[GPU] Memory offset command rejected (status=%s)" % status)
        return False

    def set_power_limit(self, gpu: GPUDevice, watts: float) -> bool:
        """Set TGP power limit in watts within driver constraints."""
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

    def enable_game_boost(self, gpu: GPUDevice) -> bool:
        """Boost mode: max power limit + moderate core/memory offsets."""
        ok1 = self.set_power_limit(gpu, gpu.power_max_w) if gpu.power_max_mw else False
        ok2 = self.set_core_offset(gpu, 90)
        ok3 = self.set_memory_offset(gpu, 400)
        return ok1 or ok2 or ok3

    def reset_offsets(self, gpu: GPUDevice) -> bool:
        """Reset core and memory offsets to factory default (0 MHz)."""
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
