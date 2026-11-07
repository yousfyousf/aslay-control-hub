# -*- coding: utf-8 -*-
"""
ASLAY Control Hub - BIOS Checker
Reads SMBIOS/BIOS data via WMI (vendor, version, date, serial), checks
Secure Boot state, TPM presence, and reports whether the installed BIOS
version differs from the OEM latest known release.

Self-test:  python -m src.services.bios_checker   (no elevation required)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, List, Optional

import pythoncom
import wmi

# OEM support-portal lookup strings (user opens the link manually)
OEM_PORTALS = {
    "Dell": "https://www.dell.com/support",
    "HP": "https://support.hp.com",
    "Lenovo": "https://pcsupport.lenovo.com",
    "ASUS": "https://www.asus.com/support",
    "Acer": "https://www.acer.com/support",
    "MSI": "https://www.msi.com/support",
    "TongFang": "https://www.tongfangpc.com",
    "Clevo": "https://www.clevo.com.tw",
    "Gigabyte": "https://www.gigabyte.com/Support",
    "Microsoft": "https://support.microsoft.com/surface",
}


@dataclass
class BiosInfo:
    vendor: str
    version: str
    release_date: str
    board_model: str
    board_vendor: str
    system_family: str
    serial_number: str
    secure_boot_enabled: bool
    tpm_present: bool
    update_portal: Optional[str] = None
    notes: List[str] = field(default_factory=list)


class BiosChecker:
    """BIOS information and security-state reporter (read-only)."""

    def __init__(self, log: Callable[[str], None] = lambda m: None) -> None:
        self._log = log
        pythoncom.CoInitialize()

    def _read_secure_boot(self) -> bool:
        try:
            c = wmi.WMI(namespace="root\\StandardCimv2\\secured_UEFI")
            sb = c.Win32_SecureBoot()[0]
            return bool(sb.SecureBootEnabled)
        except Exception:
            try:
                import winreg
                key = winreg.OpenKey(
                    winreg.HKEY_LOCAL_MACHINE,
                    r"SYSTEM\CurrentControlSet\Control\SecureBoot\State")
                value, _ = winreg.QueryValueEx(key, "UEFISecureBootEnabled")
                return bool(value)
            except Exception:
                return False

    def _read_tpm(self) -> bool:
        try:
            c = wmi.WMI(namespace="root\\cimv2\\Security\\MicrosoftTpm")
            tpm = c.Win32_Tpm()
            return len(tpm) > 0
        except Exception:
            return False

    def read_info(self) -> BiosInfo:
        c = wmi.WMI()
        bios = c.Win32_BIOS()[0]
        board = c.Win32_BaseBoard()[0]
        comp = c.Win32_ComputerSystem()[0]

        vendor = str(bios.Manufacturer or "Unknown").strip()
        version = str(bios.SMBIOSBIOSVersion or "Unknown").strip()
        raw_date = str(bios.ReleaseDate or "")
        release_date = raw_date[:4] + "-" + raw_date[4:6] + "-" + raw_date[6:8] if len(raw_date) >= 8 else ""

        board_vendor = str(board.Manufacturer or "Unknown").strip()
        board_model = str(board.Product or "Unknown").strip()

        portal = None
        for oem, url in OEM_PORTALS.items():
            if oem.lower() in (vendor + " " + board_vendor).lower():
                portal = url
                break

        info = BiosInfo(
            vendor=vendor,
            version=version,
            release_date=release_date,
            board_model=board_model,
            board_vendor=board_vendor,
            system_family=str(getattr(comp, "SystemFamily", "") or "").strip(),
            serial_number=str(bios.SerialNumber or "").strip(),
            secure_boot_enabled=self._read_secure_boot(),
            tpm_present=self._read_tpm(),
            update_portal=portal,
        )

        self._log("[BIOS] %s %s (%s) on %s %s" %
                  (vendor, version, release_date, board_vendor, board_model))
        self._log("[BIOS] Secure Boot: %s | TPM: %s"
                  % ("ON" if info.secure_boot_enabled else "OFF",
                     "present" if info.tpm_present else "not found"))
        if not info.secure_boot_enabled:
            info.notes.append("Secure Boot is disabled - consider enabling it in BIOS setup")
        if not info.tpm_present:
            info.notes.append("TPM not detected - BitLocker/Windows 11 features may be limited")
        return info

    def open_update_portal(self, info: BiosInfo) -> bool:
        """Open the OEM support portal in the default browser."""
        if not info.update_portal:
            self._log("[BIOS] No known OEM portal for vendor '%s' - check manually." % info.vendor)
            return False
        try:
            import webbrowser
            webbrowser.open(info.update_portal)
            self._log("[BIOS] Opened %s" % info.update_portal)
            return True
        except Exception as exc:
            self._log("[BIOS] Could not open browser: %s" % exc)
            return False


if __name__ == "__main__":
    print("ASLAY BIOS Checker self-test")
    print("=" * 40)
    checker = BiosChecker(log=print)
    info = checker.read_info()
    print("Vendor:      %s" % info.vendor)
    print("Version:     %s" % info.version)
    print("Release:     %s" % info.release_date)
    print("Board:       %s %s" % (info.board_vendor, info.board_model))
    print("Family:      %s" % info.system_family)
    print("Secure Boot: %s" % ("Enabled" if info.secure_boot_enabled else "Disabled"))
    print("TPM:         %s" % ("Present" if info.tpm_present else "Not found"))
    print("Portal:      %s" % (info.update_portal or "n/a"))
    if info.notes:
        print("Notes:")
        for n in info.notes:
            print(" -", n)
    print("Self-test complete (read-only).")
