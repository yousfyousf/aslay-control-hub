# -*- coding: utf-8 -*-
"""
ASLAY Control Hub - Storage Scanner
S.M.A.R.T attribute reading for NVMe SSDs and SATA HDD/SSDs, health
percentage estimation, temperature monitoring and read-only surface scan.

Self-test:  python -m src.services.storage_scanner   (requires Administrator)
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from typing import Callable, List, Optional

import pythoncom
import wmi

CRITICAL_ATTRS = {
    5: "Reallocated Sectors Count",
    9: "Power-On Hours",
    10: "Spin Retry Count",
    12: "Power Cycle Count",
    177: "Wear Range Delta",
    187: "Reported Uncorrectable Errors",
    190: "Airflow Temperature",
    194: "Temperature",
    196: "Reallocation Event Count",
    197: "Current Pending Sector Count",
    198: "Uncorrectable Sector Count",
    199: "UltraDMA CRC Error Count",
    231: "SSD Life Left",
    232: "Endurance Remaining",
    241: "Total LBAs Written",
    242: "Total LBAs Read",
}


@dataclass
class SmartAttribute:
    attr_id: int
    name: str
    raw_value: int
    value: int
    worst: int
    threshold: int
    status_ok: bool


@dataclass
class DiskHealth:
    model: str
    serial: str
    interface: str
    size_gb: float
    media_type: str
    temperature_c: Optional[int]
    health_percent: Optional[int]
    smart_status_ok: bool
    attributes: List[SmartAttribute] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


class StorageScanner:
    """Reads S.M.A.R.T data via WMI MSStorageDriver (OEM-neutral)."""

    def __init__(self, log: Callable[[str], None] = lambda m: None) -> None:
        self._log = log
        pythoncom.CoInitialize()

    def _read_smart_wmi(self) -> List[SmartAttribute]:
        """Read S.M.A.R.T via the storage namespace."""
        attrs: List[SmartAttribute] = []
        try:
            c = wmi.WMI(namespace="root\\WMI")
            instances = c.query("SELECT * FROM MSStorageDriver_FailurePredictData")
        except Exception as exc:
            self._log("[SMART] WMI storage namespace unavailable: %s" % exc)
            return attrs
        for inst in instances:
            try:
                data = list(inst.VendorSpecific or [])
            except Exception:
                continue
            if len(data) < 12:
                continue
            for i in range(0, len(data) - 12 + 1, 12):
                block = data[i:i + 12]
                attr_id = block[0]
                if attr_id == 0:
                    continue
                value = block[1]
                worst = block[2]
                threshold = block[3]
                raw_bytes = block[5:12]
                raw_value = int.from_bytes(bytes(raw_bytes[:6]), "little") if len(raw_bytes) >= 6 else 0
                name = CRITICAL_ATTRS.get(attr_id, "Attribute %d" % attr_id)
                attrs.append(SmartAttribute(
                    attr_id=attr_id, name=name, raw_value=raw_value,
                    value=value, worst=worst, threshold=threshold,
                    status_ok=value > threshold,
                ))
            break
        return attrs

    def _read_health_msonline(self, disk_index: int) -> Optional[int]:
        """Health info for physical disks via the Storage namespace."""
        try:
            c = wmi.WMI(namespace="root\\Microsoft\\Windows\\Storage")
            for pd in c.MSFT_PhysicalDisk():
                if int(pd.DeviceId or -1) == disk_index:
                    hs = pd.HealthStatus
                    mapping = {0: "Healthy", 1: "Warning", 2: "Unhealthy", 5: "Unknown"}
                    val = int(hs) if hs is not None else -1
                    return mapping.get(val, val)
        except Exception:
            pass
        return None

    def scan_all(self) -> List[DiskHealth]:
        results: List[DiskHealth] = []
        c = wmi.WMI()
        for d in c.Win32_DiskDrive():
            model = str(d.Model or "Unknown").strip()
            iface = str(d.InterfaceType or "Unknown")
            size_gb = round(int(d.Size or 0) / (1024 ** 3), 2)
            is_nvme = "nvme" in (d.Model or "").lower()
            media = "NVMe SSD" if is_nvme else "HDD/SSD"
            serial = str(d.SerialNumber or "").strip()
            temp = None
            smart_ok = True
            warnings: List[str] = []
            try:
                cw = wmi.WMI(namespace="root\\WMI")
                for st in cw.MSStorageDriver_FailurePredictStatus():
                    if st.PredictFailure:
                        smart_ok = False
                        warnings.append("S.M.A.R.T predicts failure!")
                    break
            except Exception:
                pass
            attrs = self._read_smart_wmi()
            temp_attr = next((a for a in attrs if a.attr_id in (194, 190)), None)
            if temp_attr:
                temp = temp_attr.raw_value & 0xFF
            health = self._read_health_msonline(int(d.Index or 0))
            results.append(DiskHealth(
                model=model, serial=serial, interface=iface,
                size_gb=size_gb, media_type=media,
                temperature_c=temp, health_percent=health,
                smart_status_ok=smart_ok, attributes=attrs, warnings=warnings,
            ))
            self._log("[SMART] %s (%s, %.1f GB): %d attributes read"
                      % (model, media, size_gb, len(attrs)))
        return results

    def surface_scan(self, drive_letter: str) -> dict:
        """Trigger a read-only file system scan (chkdsk /scan)."""
        try:
            result = subprocess.run(
                ["chkdsk", drive_letter + ":", "/scan"],
                capture_output=True, text=True, timeout=3600)
            return {
                "ok": result.returncode == 0,
                "output": (result.stdout or "") + (result.stderr or ""),
            }
        except Exception as exc:
            return {"ok": False, "output": str(exc)}


if __name__ == "__main__":
    print("ASLAY Storage Scanner self-test")
    print("=" * 40)
    s = StorageScanner(log=print)
    disks = s.scan_all()
    if not disks:
        print("No physical disks found.")
    for disk in disks:
        print("%s - %s - %.1f GB - Temp: %s C - Health: %s"
              % (disk.model, disk.media_type, disk.size_gb,
                 disk.temperature_c, disk.health_percent))
        if disk.warnings:
            for w in disk.warnings:
                print("  !", w)
        for a in disk.attributes[:10]:
            print("  #%d %-35s raw=%d ok=%s"
                  % (a.attr_id, a.name, a.raw_value, a.status_ok))
    print("Self-test complete (read-only, no surface scan triggered).")
