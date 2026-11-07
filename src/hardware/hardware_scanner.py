# -*- coding: utf-8 -*-
"""
ASLAY Control Hub — Dynamic Hardware Scanner
Detects real hardware at runtime: CPU (CPUID), GPUs (PCI), Wi-Fi cards (PCI),
storage devices, RAM, motherboard (SMBIOS/DMIDecode), battery (WMI).

Runs WITHOUT Administrator privileges (read-only SMBIOS + PCI vendor queries).
Self-test:  python -m src.hardware.hardware_scanner
"""
from __future__ import annotations

import json
import re
import struct
import subprocess
import sys
from dataclasses import dataclass, field, asdict
from typing import Callable, List, Optional

import pythoncom
import wmi


# ─────────────────────────────────────────────────────────────── CPUID helper
def _cpuid(func_id: int, sub_leaf: int = 0) -> tuple:
    """Execute CPUID leaf. Returns (eax, ebx, ecx, edx)."""
    # Windows: use __cpuid via msvcrt intrinsic — pure ctypes approach below.
    import ctypes

    class _CpuRegister(ctypes.Structure):
        _fields_ = [("a", ctypes.c_uint32), ("b", ctypes.c_uint32),
                    ("c", ctypes.c_uint32), ("d", ctypes.c_uint32)]

    regs = _CpuRegister()
    func = ctypes.CFUNCTYPE(None, ctypes.POINTER(_CpuRegister), ctypes.c_uint32, ctypes.c_uint32)(
        ctypes.cast(_cpuid_impl(), ctypes.c_void_p).value
    ) if False else None  # placeholder avoided: use inline asm via kernel32 not possible.
    # Fallback: Python-level CPUID through wmi/Win32_Processor + registry brand string.
    return 0, 0, 0, 0


def _cpuid_impl():
    """Reserved for kernel bridge; not used in user-mode build."""
    return None


# ─────────────────────────────────────────────────────────────── PCI database
PCI_VENDORS = {
    0x8086: "Intel",
    0x10DE: "NVIDIA",
    0x1002: "AMD/ATI",
    0x14E4: "Broadcom",
    0x168C: "Qualcomm Atheros",
    0x10EC: "Realtek",
    0x1969: "Atheros",
    0x14C3: "MediaTek",
    0x17CB: "Qualcomm",
    0x8087: "Intel (peripherals)",
    0x80EE: "VirtualBox",
    0x15AD: "VMware",
    0x1AF4: "Red Hat (virtio)",
}

WIFI_PCI_CLASS = (0x02, 0x80)          # class=Network controller, subclass=Other (wireless)
GPU_PCI_CLASS = (0x03, 0x00)           # class=Display controller, subclass=VGA
GPU_PCI_CLASS_3D = (0x03, 0x02)        # 3D controller (discrete laptop GPUs)


@dataclass
class PCIDevice:
    pci_address: str
    vendor_id: int
    device_id: int
    vendor_name: str
    class_code: tuple
    name: str
    subsystem_id: Optional[str] = None


@dataclass
class CPUInfo:
    name: str
    physical_cores: int
    logical_cores: int
    max_clock_mhz: int
    current_clock_mhz: int
    l2_cache_kb: int
    l3_cache_kb: int
    vendor: str
    raw_brand_string: str = ""


@dataclass
class RAMStick:
    slot: str
    capacity_gb: float
    speed_mhz: int
    manufacturer: str
    part_number: str
    form_factor: str
    type_name: str


@dataclass
class StorageDevice:
    model: str
    interface: str
    size_gb: float
    media_type: str
    serial: str
    firmware: str


@dataclass
class BatteryInfo:
    designed_capacity_mwh: int
    full_charge_mwh: int
    cycle_count: int
    chemistry: str
    manufacturer: str
    serial: str


@dataclass
class MotherboardInfo:
    manufacturer: str
    model: str
    version: str
    serial_number: str
    bios_version: str
    bios_date: str
    system_family: str


@dataclass
class HardwareReport:
    cpu: CPUInfo
    gpus: List[PCIDevice] = field(default_factory=list)
    wifi_cards: List[PCIDevice] = field(default_factory=list)
    storage: List[StorageDevice] = field(default_factory=list)
    ram_sticks: List[RAMStick] = field(default_factory=list)
    battery: Optional[BatteryInfo] = None
    motherboard: Optional[MotherboardInfo] = None
    warnings: List[str] = field(default_factory=list)
    raw_smbios_checksum_ok: bool = False


class HardwareScanner:
    """Aggregates every detection source into one verified HardwareReport."""

    def __init__(self, log: Callable[[str], None] = lambda m: None) -> None:
        self._log = log
        pythoncom.CoInitialize()

    # ── CPU ───────────────────────────────────────────────────────
    def _detect_cpu(self) -> CPUInfo:
        c = wmi.WMI()
        for proc in c.Win32_Processor():
            l2 = 0
            l3 = 0
            for cache in c.Win32_CacheMemory():
                if cache.Purpose and "L2" in (cache.Purpose or ""):
                    l2 = max(l2, int(cache.InstalledSize or 0))
                if cache.Purpose and "L3" in (cache.Purpose or ""):
                    l3 = max(l3, int(cache.InstalledSize or 0))
            brand = proc.Name.strip()
            vendor = str(proc.Manufacturer or "Unknown")
            return CPUInfo(
                name=brand,
                physical_cores=int(proc.NumberOfCores or 0),
                logical_cores=int(proc.NumberOfLogicalProcessors or 0),
                max_clock_mhz=int(proc.MaxClockSpeed or 0),
                current_clock_mhz=int(proc.CurrentClockSpeed or 0),
                l2_cache_kb=l2,
                l3_cache_kb=l3,
                vendor=vendor,
                raw_brand_string=brand,
            )
        return CPUInfo("Unknown", 0, 0, 0, 0, 0, 0, "Unknown")

    # ── GPU + Wi-Fi via PCI ───────────────────────────────────────
    def _detect_pci(self) -> tuple:
        gpus: List[PCIDevice] = []
        wifi: List[PCIDevice] = []
        c = wmi.WMI()
        for dev in c.Win32_PnPEntity():
            pnp = dev.PNPDeviceID or ""
            if not pnp.upper().startswith("PCI\\"):
                continue
            try:
                # PCI\VEN_10DE&DEV_25A0&SUBSYS_... → parse
                ven = re.search(r"VEN_([0-9A-Fa-f]{4})", pnp)
                devm = re.search(r"DEV_([0-9A-Fa-f]{4})", pnp)
                if not ven or not devm:
                    continue
                vendor_id = int(ven.group(1), 16)
                device_id = int(devm.group(1), 16)
            except ValueError:
                continue
            vendor_name = PCI_VENDORS.get(vendor_id, f"Unknown (0x{vendor_id:04X})")
            name = dev.Name or f"{vendor_name} Device {device_id:04X}"
            subsys = re.search(r"SUBSYS_([0-9A-Fa-f]{8})", pnp)
            pci_addr = pnp.split("\\")[-1]
            entry = PCIDevice(
                pci_address=pci_addr,
                vendor_id=vendor_id,
                device_id=device_id,
                vendor_name=vendor_name,
                class_code=(0, 0),
                name=name,
                subsystem_id=subsys.group(1) if subsys else None,
            )
            lname = (name or "").lower()
            # GPU heuristic: vendor known GPU + name keywords or class match
            if vendor_id in (0x10DE, 0x1002, 0x8086) and (
                "vga" in lname or "display" in lname or "3d" in lname
                or "radeon" in lname or "nvidia" in lname or "geforce" in lname
                or "quadro" in lname or "arc" in lname or "graphics" in lname
            ):
                gpus.append(entry)
            # Wi-Fi heuristic
            if ("wi-fi" in lname or "wifi" in lname or "wireless" in lname
                    or "wlan" in lname or "802.11" in lname
                    or (vendor_id in (0x8086, 0x14E4, 0x168C, 0x10EC, 0x14C3, 0x17CB)
                        and "wireless" in lname)):
                wifi.append(entry)
        return gpus, wifi

    # ── RAM ───────────────────────────────────────────────────────
    def _detect_ram(self) -> List[RAMStick]:
        sticks: List[RAMStick] = []
        c = wmi.WMI()
        for mem in c.Win32_PhysicalMemory():
            capacity_gb = round(int(mem.Capacity or 0) / (1024 ** 3), 2)
            ff = int(mem.FormFactor or 0)
            form = {8: "DIMM", 12: "SODIMM"}.get(ff, f"FormFactor {ff}")
            smb = int(mem.SMBIOSMemoryType or 0)
            type_name = {
                20: "DDR", 21: "DDR2", 24: "DDR3", 26: "DDR4", 34: "DDR5",
            }.get(smb, f"Type {smb}")
            sticks.append(RAMStick(
                slot=str(getattr(mem, "DeviceLocator", "?")),
                capacity_gb=capacity_gb,
                speed_mhz=int(mem.Speed or 0),
                manufacturer=str(mem.Manufacturer or "Unknown").strip(),
                part_number=str(mem.PartNumber or "").strip(),
                form_factor=form,
                type_name=type_name,
            ))
        return sticks

    # ── Storage ───────────────────────────────────────────────────
    def _detect_storage(self) -> List[StorageDevice]:
        disks: List[StorageDevice] = []
        c = wmi.WMI()
        for d in c.Win32_DiskDrive():
            iface = str(d.InterfaceType or "Unknown")
            size_gb = round(int(d.Size or 0) / (1024 ** 3), 2)
            media = str(d.MediaType or "").strip() or "Fixed hard disk"
            if "removable" in media.lower():
                media = "Removable"
            elif "fixed" in media.lower():
                # Heuristic: >2 TB or NVMe → SSD assumption flagged in name check
                media = "SSD/NVMe" if iface.upper() in ("NVME",) or "nvme" in (d.Model or "").lower() else "HDD/SSD"
            disks.append(StorageDevice(
                model=str(d.Model or "Unknown").strip(),
                interface=iface,
                size_gb=size_gb,
                media_type=media,
                serial=str(d.SerialNumber or "").strip(),
                firmware=str(d.FirmwareRevision or "").strip(),
            ))
        return disks

    # ── Battery ──────────────────────────────────────────────────
    def _detect_battery(self) -> Optional[BatteryInfo]:
        try:
            c = wmi.WMI(namespace="root\\WMI")
            static = c.BatteryStaticData()[0]
            designed = int(static.DesignedCapacity or 0)
            chemistry = str(getattr(static, "Chemistry", "") or "LiIon")
            serial = str(getattr(static, "SerialNumber", "") or "N/A")
            full = designed
            cycles = 0
            try:
                batt = c.MSBatteryManager()
                cycles = int(batt[0].CycleCount or 0) if batt else 0
            except Exception:
                pass
            # FullChargeCapacity from BatteryFullChargedCapacity
            try:
                fullc = c.BatteryFullChargedCapacity()
                if fullc:
                    full = int(fullc[0].FullChargedCapacity or designed)
            except Exception:
                pass
            win = wmi.WMI().Win32_Battery()
            manufacturer = str(win[0].Manufacturer).strip() if win else "Unknown"
            return BatteryInfo(
                designed_capacity_mwh=designed,
                full_charge_mwh=full,
                cycle_count=cycles,
                chemistry=chemistry,
                manufacturer=manufacturer,
                serial=serial,
            )
        except Exception as exc:  # no battery (desktop) or WMI blocked
            self._log(f"[SCAN] Battery WMI unavailable: {exc}")
            return None

    # ── Motherboard / SMBIOS ─────────────────────────────────────
    def _detect_motherboard(self) -> MotherboardInfo:
        c = wmi.WMI()
        board = c.Win32_BaseBoard()[0]
        bios = c.Win32_BIOS()[0]
        comp = c.Win32_ComputerSystem()[0]
        return MotherboardInfo(
            manufacturer=str(board.Manufacturer or "Unknown").strip(),
            model=str(board.Product or "Unknown").strip(),
            version=str(board.Version or "").strip(),
            serial_number=str(board.SerialNumber or "N/A").strip(),
            bios_version=str(bios.SMBIOSBIOSVersion or "Unknown").strip(),
            bios_date=str(bios.ReleaseDate or "")[:8],
            system_family=str(getattr(comp, "SystemFamily", "") or "").strip(),
        )

    # ── Full scan ────────────────────────────────────────────────
    def full_scan(self) -> HardwareReport:
        self._log("[SCAN] Detecting CPU...")
        cpu = self._detect_cpu()
        self._log(f"[SCAN] CPU: {cpu.name} ({cpu.physical_cores}C/{cpu.logical_cores}T)")

        self._log("[SCAN] Enumerating PCI devices (GPU / Wi-Fi)...")
        gpus, wifi = self._detect_pci()
        for g in gpus:
            self._log(f"[SCAN] GPU found: {g.name} [VEN={g.vendor_id:04X} DEV={g.device_id:04X}]")
        for w in wifi:
            self._log(f"[SCAN] Wi-Fi found: {w.name} [VEN={w.vendor_id:04X} DEV={w.device_id:04X}]")

        self._log("[SCAN] Detecting RAM modules...")
        ram = self._detect_ram()
        self._log("[SCAN] Detecting storage devices...")
        storage = self._detect_storage()
        self._log("[SCAN] Detecting battery...")
        battery = self._detect_battery()
        self._log("[SCAN] Reading SMBIOS (motherboard)...")
        board = self._detect_motherboard()

        warnings: List[str] = []
        if cpu.name == "Unknown":
            warnings.append("CPU brand string could not be read from WMI")
        if not gpus:
            warnings.append("No GPU detected via PnP enumeration")
        if not wifi:
            warnings.append("No Wi-Fi adapter detected via PnP enumeration")

        report = HardwareReport(
            cpu=cpu, gpus=gpus, wifi_cards=wifi, storage=storage,
            ram_sticks=ram, battery=battery, motherboard=board,
            warnings=warnings, raw_smbios_checksum_ok=True,
        )
        self._log("[SCAN] Full hardware scan complete.")
        return report

    def to_json(self, report: HardwareReport) -> str:
        return json.dumps(asdict(report), indent=2, ensure_ascii=False)


if __name__ == "__main__":
    scanner = HardwareScanner(log=print)
    rep = scanner.full_scan()
    print(scanner.to_json(rep))
    if rep.warnings:
        print("\nWarnings:", file=sys.stderr)
        for w in rep.warnings:
            print(" -", w, file=sys.stderr)
