# -*- coding: utf-8 -*-
"""
ASLAY Control Hub - Wi-Fi Booster
Dynamic wireless adapter optimization: power plan, roaming aggressiveness,
band preference (5 GHz), adapter power management and wireless mode.
Targets Intel AX family (AX200/AX201/AX210/AX211/BE200) with generic
fallback for Broadcom/Realtek/MediaTek adapters.

Self-test:  python -m src.services.wifi_booster   (requires Administrator)
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from typing import Callable, List

import pythoncom
import wmi

INTEL_AX_DEV_IDS = {
    0x2723: "Wi-Fi 6 AX200",
    0xA0F0: "Wi-Fi 6 AX201",
    0x2725: "Wi-Fi 6E AX210",
    0x51F0: "Wi-Fi 6E AX211",
    0x51F1: "Wi-Fi 6E AX211 (alt)",
    0x7F70: "Wi-Fi 7 BE200",
}

HIGH_PERF_PLAN = "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"
BALANCED_PLAN = "381b4222-f694-41f0-9685-ff5bb260df2e"


@dataclass
class WiFiAdapter:
    name: str
    description: str
    pnp_id: str
    vendor_id: int
    device_id: int
    driver_version: str
    is_intel_ax: bool


@dataclass
class BoosterResult:
    adapter: str
    applied: List[str] = field(default_factory=list)
    failed: List[str] = field(default_factory=list)


class WiFiBooster:
    """Optimizes Wi-Fi adapter settings for throughput and latency."""

    def __init__(self, log: Callable[[str], None] = lambda m: None) -> None:
        self._log = log
        pythoncom.CoInitialize()

    def detect_adapters(self) -> List[WiFiAdapter]:
        adapters: List[WiFiAdapter] = []
        c = wmi.WMI()
        for dev in c.Win32_NetworkAdapter():
            pnp = dev.PNPDeviceID or ""
            if not pnp.upper().startswith("PCI\\"):
                continue
            desc = (dev.Description or "").lower()
            if not ("wi-fi" in desc or "wifi" in desc or "wireless" in desc
                    or "wlan" in desc or "802.11" in desc):
                continue
            ven = re.search(r"VEN_([0-9A-Fa-f]{4})", pnp)
            devm = re.search(r"DEV_([0-9A-Fa-f]{4})", pnp)
            vendor_id = int(ven.group(1), 16) if ven else 0
            device_id = int(devm.group(1), 16) if devm else 0
            driver_ver = ""
            try:
                for d in c.Win32_PnPSignedDriver():
                    if d.PNPDeviceID == pnp:
                        driver_ver = str(d.DriverVersion or "")
                        break
            except Exception:
                pass
            adapters.append(WiFiAdapter(
                name=dev.NetConnectionID or dev.Name or "Wi-Fi",
                description=dev.Description or "Unknown",
                pnp_id=pnp,
                vendor_id=vendor_id,
                device_id=device_id,
                driver_version=driver_ver,
                is_intel_ax=(vendor_id == 0x8086 and device_id in INTEL_AX_DEV_IDS),
            ))
        for a in adapters:
            self._log("[WIFI] Found: %s [%04X:%04X]%s"
                      % (a.description, a.vendor_id, a.device_id,
                         " (Intel AX family)" if a.is_intel_ax else ""))
        return adapters

    # ── individual optimizations ─────────────────────────────────
    def _run(self, args, shell: bool = False) -> bool:
        try:
            result = subprocess.run(args, capture_output=True, text=True,
                                     timeout=30, shell=shell)
            return result.returncode == 0
        except Exception as exc:
            self._log("[WIFI] command failed: %s" % exc)
            return False

    def _ps(self, command: str) -> bool:
        return self._run(
            "powershell -NoProfile -Command \"%s\"" % command, shell=True)

    def _set_power_plan(self, guid: str, label: str) -> bool:
        ok = self._run(["powercfg", "/setactive", guid])
        self._log("[WIFI] %s power plan: %s" % (label, "OK" if ok else "FAILED"))
        return ok

    def _disable_adapter_power_saving(self, adapter: WiFiAdapter) -> bool:
        ok = self._ps(
            "Set-NetAdapterPowerManagement -Name '%s' "
            "-AllowComputerToTurnOffDevice Disabled" % adapter.name)
        if ok:
            self._log("[WIFI] Adapter power saving disabled: %s" % adapter.name)
        else:
            self._log("[WIFI] Adapter power saving toggle FAILED (OEM driver may hide it)")
        return ok

    def _set_roaming_aggressive(self, adapter: WiFiAdapter) -> bool:
        ok = self._ps(
            "Set-NetAdapterAdvancedProperty -Name '%s' "
            "-DisplayName 'Roaming Aggressiveness' -DisplayValue '4. Highest'"
            % adapter.name)
        if not ok:
            ok = self._ps(
                "Set-NetAdapterAdvancedProperty -Name '%s' "
                "-DisplayName 'Roaming Aggressiveness' -DisplayValue 'Highest'"
                % adapter.name)
        self._log("[WIFI] Roaming aggressiveness: %s" % ("OK" if ok else "FAILED"))
        return ok

    def _prefer_5ghz(self, adapter: WiFiAdapter) -> bool:
        ok = self._ps(
            "Set-NetAdapterAdvancedProperty -Name '%s' "
            "-DisplayName 'Preferred Band' -DisplayValue 'Prefer 5GHz band'"
            % adapter.name)
        if not ok:
            ok = self._ps(
                "Set-NetAdapterAdvancedProperty -Name '%s' "
                "-DisplayName 'Preferred Band' -DisplayValue '3. Prefer 5GHz band'"
                % adapter.name)
        self._log("[WIFI] Prefer 5 GHz: %s" % ("OK" if ok else "FAILED"))
        return ok

    def _set_wireless_mode_max(self, adapter: WiFiAdapter) -> bool:
        ok = self._ps(
            "Set-NetAdapterAdvancedProperty -Name '%s' "
            "-DisplayName 'Wireless Mode' -DisplayValue '6. 802.11ax'"
            % adapter.name)
        if not ok:
            ok = self._ps(
                "Set-NetAdapterAdvancedProperty -Name '%s' "
                "-DisplayName '802.11n/ac/ax Wireless Mode' "
                "-DisplayValue '6. 802.11ax'" % adapter.name)
        self._log("[WIFI] Max wireless mode: %s" % ("OK" if ok else "FAILED"))
        return ok

    # ── full boost / restore ─────────────────────────────────────
    def apply_boost(self, adapter: WiFiAdapter) -> BoosterResult:
        result = BoosterResult(adapter=adapter.description)
        steps = [
            ("High performance power plan",
             lambda: self._set_power_plan(HIGH_PERF_PLAN, "High performance")),
            ("Disable adapter power saving",
             lambda: self._disable_adapter_power_saving(adapter)),
            ("Aggressive roaming",
             lambda: self._set_roaming_aggressive(adapter)),
            ("Prefer 5 GHz band",
             lambda: self._prefer_5ghz(adapter)),
            ("Max wireless mode",
             lambda: self._set_wireless_mode_max(adapter)),
        ]
        for label, fn in steps:
            try:
                if fn():
                    result.applied.append(label)
                else:
                    result.failed.append(label)
            except Exception as exc:
                result.failed.append(label)
                self._log("[WIFI] ERROR in %s: %s" % (label, exc))
        return result

    def restore_default(self, adapter: WiFiAdapter) -> BoosterResult:
        """Undo the boost: balanced plan + adapter power saving on."""
        result = BoosterResult(adapter=adapter.description)
        if self._set_power_plan(BALANCED_PLAN, "Balanced"):
            result.applied.append("Balanced power plan")
        else:
            result.failed.append("Balanced power plan")
        if self._ps(
                "Set-NetAdapterPowerManagement -Name '%s' "
                "-AllowComputerToTurnOffDevice Enabled" % adapter.name):
            result.applied.append("Adapter power saving re-enabled")
        else:
            result.failed.append("Adapter power saving re-enabled")
        if self._ps(
                "Set-NetAdapterAdvancedProperty -Name '%s' "
                "-DisplayName 'Roaming Aggressiveness' -DisplayValue '2. Medium'"
                % adapter.name):
            result.applied.append("Roaming back to medium")
        else:
            result.failed.append("Roaming back to medium")
        return result


if __name__ == "__main__":
    print("ASLAY Wi-Fi Booster self-test")
    print("=" * 40)
    b = WiFiBooster(log=print)
    adapters = b.detect_adapters()
    if not adapters:
        print("No wireless PCI adapters found.")
        raise SystemExit(0)
    target = adapters[0]
    print("Applying boost to: %s" % target.description)
    res = b.apply_boost(target)
    print("Applied %d steps, %d failed" % (len(res.applied), len(res.failed)))
    for s in res.applied:
        print("  +", s)
    for s in res.failed:
        print("  -", s)
    print("Self-test complete. Use restore_default() to undo.")
