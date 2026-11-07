# -*- coding: utf-8 -*-
"""
ASLAY Control Hub - Embedded Controller (EC) Driver
Battery charge limiter (60% / 80% / 100%) via raw port I/O on ports 0x62/0x66.
Uses WinRing0x64.dll (ring-0 bridge) placed next to project root.
Falls back gracefully to WMI read-only mode when ring-0 access is unavailable.

Self-test:  python -m src.hardware.ec_driver   (requires Administrator)
"""
from __future__ import annotations

import ctypes
import os
import sys
import time
from dataclasses import dataclass
from typing import Callable, Optional

import pythoncom
import wmi

EC_DATA_PORT = 0x62
EC_INDEX_PORT = 0x66

EC_OBF = 0x01  # Output buffer full - data readable
EC_IBF = 0x02  # Input buffer full - cannot write yet

EC_CMD_READ = 0x80
EC_CMD_WRITE = 0x81

# Common OEM EC RAM offsets for battery charge control (probed dynamically)
BATTERY_LIMIT_OFFSETS = [0xB0, 0xB1, 0xB2, 0xD0, 0xD2, 0xE0, 0xE1, 0xE2, 0xFA, 0xFB]

DLL_NAME = "WinRing0x64.dll"


class WinRing0:
    """Minimal ctypes wrapper around WinRing0x64.dll ring-0 port I/O."""

    def __init__(self, log: Callable[[str], None]) -> None:
        self._log = log
        self._dll = None
        self._initialized = False

    def _find_dll(self) -> Optional[str]:
        bases = [
            os.path.dirname(os.path.abspath(sys.argv[0])) if sys.argv and sys.argv[0] else None,
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
        ]
        for base in bases:
            if not base:
                continue
            p = os.path.join(base, DLL_NAME)
            if os.path.exists(p):
                return p
        return None

    def initialize(self) -> bool:
        path = self._find_dll()
        if not path:
            self._log("[EC] WinRing0x64.dll not found next to project root - WMI fallback only.")
            return False
        try:
            self._dll = ctypes.WinDLL(path)
        except OSError as exc:
            self._log("[EC] Failed loading DLL: %s" % exc)
            return False
        try:
            self._dll.InitializeOls()
            if self._dll.GetDllStatus() != 0:
                self._log("[EC] WinRing0 InitializeOls failed (driver not loaded).")
                self._dll = None
                return False
            self._initialized = True
            self._log("[EC] WinRing0 ring-0 bridge loaded: %s" % path)
            return True
        except Exception as exc:
            self._log("[EC] WinRing0 init error: %s" % exc)
            self._dll = None
            return False

    def shutdown(self) -> None:
        if self._dll and self._initialized:
            try:
                self._dll.DeinitOls()
            except Exception:
                pass
        self._initialized = False
        self._dll = None

    def read_port_byte(self, port: int) -> int:
        if not self._initialized:
            return -1
        return self._dll.ReadIoPortByte(ctypes.c_ushort(port))

    def write_port_byte(self, port: int, value: int) -> bool:
        if not self._initialized:
            return False
        self._dll.WriteIoPortByte(ctypes.c_ushort(port), ctypes.c_uchar(value))
        return True

    @property
    def available(self) -> bool:
        return self._initialized


@dataclass
class BatteryLimitsStatus:
    battery_present: bool
    charge_limit_percent: Optional[int]
    design_capacity_mwh: int
    full_charge_mwh: int
    current_capacity_mwh: int
    charge_rate_mw: int
    discharge_rate_mw: int
    voltage_mv: int
    cycle_count: int
    ec_writable: bool
    source: str = "wmi"


class ECDriver:
    """EC access facade with WMI read fallback when ring-0 is unavailable."""

    def __init__(self, log: Callable[[str], None] = lambda m: None) -> None:
        self._log = log
        self._ring = WinRing0(log)
        self._com_ready = False

    def start(self) -> bool:
        pythoncom.CoInitialize()
        self._com_ready = True
        return self._ring.initialize()

    def stop(self) -> None:
        self._ring.shutdown()
        if self._com_ready:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass
            self._com_ready = False

    def _ec_wait_ibf_clear(self, timeout_ms: int = 10) -> bool:
        deadline = time.perf_counter() + timeout_ms / 1000.0
        while time.perf_counter() < deadline:
            status = self._ring.read_port_byte(EC_INDEX_PORT)
            if status >= 0 and not (status & EC_IBF):
                return True
            time.sleep(0.0005)
        return False

    def _ec_wait_obf_set(self, timeout_ms: int = 10) -> bool:
        deadline = time.perf_counter() + timeout_ms / 1000.0
        while time.perf_counter() < deadline:
            status = self._ring.read_port_byte(EC_INDEX_PORT)
            if status >= 0 and (status & EC_OBF):
                return True
            time.sleep(0.0005)
        return False

    def _ec_read(self, offset: int) -> Optional[int]:
        if not self._ring.available:
            return None
        if not self._ec_wait_ibf_clear():
            return None
        self._ring.write_port_byte(EC_INDEX_PORT, EC_CMD_READ)
        if not self._ec_wait_ibf_clear():
            return None
        self._ring.write_port_byte(EC_INDEX_PORT, offset)
        if not self._ec_wait_obf_set():
            return None
        return self._ring.read_port_byte(EC_DATA_PORT)

    def _ec_write(self, offset: int, value: int) -> bool:
        if not self._ring.available:
            return False
        if not self._ec_wait_ibf_clear():
            return False
        self._ring.write_port_byte(EC_INDEX_PORT, EC_CMD_WRITE)
        if not self._ec_wait_ibf_clear():
            return False
        self._ring.write_port_byte(EC_INDEX_PORT, offset)
        if not self._ec_wait_ibf_clear():
            return False
        self._ring.write_port_byte(EC_DATA_PORT, value & 0xFF)
        return True

    def read_limits_status(self) -> BatteryLimitsStatus:
        try:
            c = wmi.WMI(namespace="root\\WMI")
            static = c.BatteryStaticData()[0]
            designed = int(static.DesignedCapacity or 0)
            full = designed
            try:
                fullc = c.BatteryFullChargedCapacity()
                if fullc:
                    full = int(fullc[0].FullChargedCapacity or designed)
            except Exception:
                pass
            status = c.BatteryStatus()
            rate = 0
            voltage = 0
            charging = False
            if status:
                s = status[0]
                rate = int(s.Rate or 0)
                voltage = int(s.Voltage or 0)
                charging = bool(getattr(s, "PowerOnline", False) and getattr(s, "Charging", False))
            cur = wmi.WMI().Win32_Battery()
            pct = int(cur[0].EstimatedChargeRemaining) if cur else 0
            cur_mwh = int(full * pct / 100) if full else 0
            cycles = 0
            try:
                bm = c.MSBatteryManager()
                cycles = int(bm[0].CycleCount or 0) if bm else 0
            except Exception:
                pass
            limit: Optional[int] = None
            if self._ring.available:
                for off in BATTERY_LIMIT_OFFSETS:
                    v = self._ec_read(off)
                    if v is not None and v in (60, 80, 100):
                        limit = v
                        break
            return BatteryLimitsStatus(
                battery_present=True,
                charge_limit_percent=limit,
                design_capacity_mwh=designed,
                full_charge_mwh=full,
                current_capacity_mwh=cur_mwh,
                charge_rate_mw=rate if charging else 0,
                discharge_rate_mw=abs(rate) if (not charging and rate < 0) else 0,
                voltage_mv=voltage,
                cycle_count=cycles,
                ec_writable=self._ring.available,
                source="ec+wmi" if self._ring.available else "wmi",
            )
        except Exception as exc:
            self._log("[EC] WMI battery read failed: %s" % exc)
            return BatteryLimitsStatus(
                battery_present=False, charge_limit_percent=None,
                design_capacity_mwh=0, full_charge_mwh=0, current_capacity_mwh=0,
                charge_rate_mw=0, discharge_rate_mw=0, voltage_mv=0,
                cycle_count=0, ec_writable=self._ring.available,
            )

    def set_charge_limit(self, percent: int) -> bool:
        """Set battery charge ceiling. Valid: 60, 80, 100 (100 = cancel limit)."""
        if percent not in (60, 80, 100):
            raise ValueError("Charge limit must be one of 60, 80, 100")
        if not self._ring.available:
            self._log("[EC] Ring-0 bridge unavailable - cannot write charge limit (WMI-only mode).")
            return False
        raw = 0 if percent == 100 else percent
        written_any = False
        for off in BATTERY_LIMIT_OFFSETS:
            cur = self._ec_read(off)
            if cur is not None and cur in (0, 60, 80, 100):
                if self._ec_write(off, raw):
                    written_any = True
                    self._log("[EC] Charge limit %d%% written to EC RAM 0x%02X" % (percent, off))
        if written_any:
            time.sleep(0.05)
            for off in BATTERY_LIMIT_OFFSETS:
                v = self._ec_read(off)
                if v is not None and v == raw:
                    return True
            self._log("[EC] Write not verified - EC may reject this offset on this OEM.")
            return False
        self._log("[EC] No known EC RAM offset accepted a charge-limit write on this machine.")
        return False


if __name__ == "__main__":
    print("ASLAY EC Driver self-test")
    print("=" * 40)
    ec = ECDriver(log=print)
    ring_ok = ec.start()
    print("Ring-0 bridge: %s" % ("ACTIVE" if ring_ok else "NOT ACTIVE (WMI fallback)"))
    st = ec.read_limits_status()
    print("Battery present: %s" % st.battery_present)
    print("Design capacity: %s mWh" % st.design_capacity_mwh)
    print("Full charge capacity: %s mWh" % st.full_charge_mwh)
    print("Charge rate: %s mW / Discharge: %s mW" % (st.charge_rate_mw, st.discharge_rate_mw))
    print("Cycle count: %s" % st.cycle_count)
    print("EC writable: %s" % st.ec_writable)
    print("Source: %s" % st.source)
    if ring_ok:
        vals = []
        for off in range(0x00, 0x10):
            v = ec._ec_read(off)
            vals.append("%02X" % v if v is not None else "??")
        print("EC RAM[0x00-0x0F]: %s" % " ".join(vals))
    ec.stop()
    print("Self-test complete. No writes were performed.")
