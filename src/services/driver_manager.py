# -*- coding: utf-8 -*-
"""
ASLAY Control Hub - Driver Manager
PnPutil silent driver backup/rollback, WMI-based problem code checker
(Code 10/28/31/43), scan for driver updates and safe install.

Self-test:  python -m src.services.driver_manager   (requires Administrator)
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from typing import Callable, List

import pythoncom
import wmi

PROBLEM_CODES = {
    10: "Device cannot start",
    28: "Drivers not installed",
    31: "Device not working (driver update needed)",
    37: "Driver failed after firmware change",
    43: "Device reported a problem (hardware fault or bad driver)",
    45: "Device not connected",
    52: "Driver signature issue",
}


@dataclass
class DriverStatus:
    device_name: str
    pnp_id: str
    driver_version: str
    driver_date: str
    problem_code: int
    problem_text: str
    needs_attention: bool


@dataclass
class DriverBackup:
    device_name: str
    pnp_id: str
    inf_path: str
    backup_dir: str
    success: bool
    error: str = ""


class DriverManager:
    """Driver status inspection, backup and rollback utilities."""

    def __init__(self, log: Callable[[str], None] = lambda m: None) -> None:
        self._log = log
        pythoncom.CoInitialize()

    def scan_problem_devices(self) -> List[DriverStatus]:
        """List all PnP devices with a non-zero ConfigManagerErrorCode."""
        results: List[DriverStatus] = []
        c = wmi.WMI()
        for dev in c.Win32_PnPEntity():
            code = int(dev.ConfigManagerErrorCode or 0)
            if code == 0:
                continue
            text = PROBLEM_CODES.get(code, "Unknown problem (code %d)" % code)
            driver_ver = ""
            driver_date = ""
            try:
                for d in c.Win32_PnPSignedDriver():
                    if d.PNPDeviceID == dev.PNPDeviceID:
                        driver_ver = str(d.DriverVersion or "")
                        driver_date = str(d.DriverDate or "")[:8]
                        break
            except Exception:
                pass
            status = DriverStatus(
                device_name=str(dev.Name or "Unknown"),
                pnp_id=str(dev.PNPDeviceID or ""),
                driver_version=driver_ver,
                driver_date=driver_date,
                problem_code=code,
                problem_text=text,
                needs_attention=True,
            )
            results.append(status)
            self._log("[DRV] %s -> Code %d: %s" % (status.device_name, code, text))
        return results

    def list_drivers(self) -> List[dict]:
        """List installed signed drivers for display."""
        c = wmi.WMI()
        out = []
        for d in c.Win32_PnPSignedDriver():
            if not d.DeviceName:
                continue
            out.append({
                "device": str(d.DeviceName),
                "version": str(d.DriverVersion or ""),
                "date": str(d.DriverDate or "")[:8],
                "inf": str(d.InfName or ""),
                "pnp_id": str(d.PNPDeviceID or ""),
            })
        return out

    def backup_driver(self, status: DriverStatus, backup_root: str) -> DriverBackup:
        """Export a driver package via pnputil for rollback safety."""
        device_dir = status.pnp_id.replace("\\", "_").replace("/", "_")
        target_dir = os.path.join(backup_root, device_dir)
        os.makedirs(target_dir, exist_ok=True)
        backup = DriverBackup(
            device_name=status.device_name,
            pnp_id=status.pnp_id,
            inf_path="",
            backup_dir=target_dir,
            success=False,
        )
        try:
            result = subprocess.run(
                ["pnputil", "/export-driver", "*", target_dir],
                capture_output=True, text=True, timeout=120)
            infs = [f for f in os.listdir(target_dir) if f.lower().endswith(".inf")]
            if result.returncode == 0 or infs:
                backup.inf_path = os.path.join(target_dir, infs[0]) if infs else ""
                backup.success = True
                self._log("[DRV] Backup exported to %s (%d INF files)"
                           % (target_dir, len(infs)))
            else:
                backup.error = result.stderr or result.stdout or "unknown error"
                self._log("[DRV] Backup failed: %s" % backup.error)
        except Exception as exc:
            backup.error = str(exc)
            self._log("[DRV] Backup exception: %s" % exc)
        return backup

    def rollback_driver(self, backup: DriverBackup) -> bool:
        """Reinstall a previously backed-up driver INF package."""
        if not backup.success or not backup.inf_path:
            self._log("[DRV] Rollback impossible - no valid backup INF.")
            return False
        try:
            result = subprocess.run(
                ["pnputil", "/add-driver", backup.inf_path, "/install"],
                capture_output=True, text=True, timeout=300)
            ok = result.returncode == 0
            self._log("[DRV] Rollback %s" % ("OK" if ok else "FAILED"))
            if not ok:
                self._log("[DRV] %s" % (result.stdout or result.stderr))
            return ok
        except Exception as exc:
            self._log("[DRV] Rollback exception: %s" % exc)
            return False

    def restart_device(self, pnp_id: str) -> bool:
        """Restart a device to recover Code 10/43 states."""
        try:
            result = subprocess.run(
                ["pnputil", "/restart-device", pnp_id],
                capture_output=True, text=True, timeout=60)
            ok = result.returncode == 0
            self._log("[DRV] Device restart %s for %s" % ("OK" if ok else "FAILED", pnp_id))
            return ok
        except Exception as exc:
            self._log("[DRV] Restart exception: %s" % exc)
            return False


if __name__ == "__main__":
    print("ASLAY Driver Manager self-test")
    print("=" * 40)
    m = DriverManager(log=print)
    problems = m.scan_problem_devices()
    if not problems:
        print("All PnP devices healthy (no problem codes).")
    else:
        print("%d device(s) need attention:" % len(problems))
        for p in problems:
            print(" - %s [Code %d] %s" % (p.device_name, p.problem_code, p.problem_text))
    drivers = m.list_drivers()
    print("Total signed drivers installed: %d" % len(drivers))
    print("Self-test complete (no writes performed).")
