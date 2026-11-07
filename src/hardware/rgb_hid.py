# -*- coding: utf-8 -*-
"""
ASLAY Control Hub - RGB HID Controller
Keyboard backlight control for ITE 8291 / Uniwill RGB controllers via
USB HID feature reports (64-byte). Query commands do not write permanent
state; effect commands change keyboard lighting.

Self-test:  python -m src.hardware.rgb_hid   (requires Administrator)
"""
from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from dataclasses import dataclass
from typing import Callable, List, Optional

# Known RGB controller USB VID/PID pairs (dynamically verified at runtime)
RGB_CONTROLLER_IDS = {
    (0x048D, 0x5702): "ITE 8291 RGB",
    (0x048D, 0xCE00): "ITE 8291 RGB (alt)",
    (0x048D, 0x8297): "ITE 8297 RGB",
    (0x1A2C, 0x4455): "TongFang RGB",
    (0x195D, 0x7008): "Clevo TongFang RGB",
    (0x0B05, 0x1869): "ASUS ITE RGB",
}

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
FILE_SHARE_READ = 1
FILE_SHARE_WRITE = 2

# Report IDs commonly used by ITE 8291 family (probed in order)
CANDIDATE_REPORT_IDS = [0x00, 0x08, 0xCD]

# Effect modes
MODE_STATIC = "static"
MODE_BREATHING = "breathing"
MODE_RAINBOW = "rainbow"
MODE_WAVE = "wave"
MODE_OFF = "off"

MODE_CODES = {
    MODE_OFF: 0x00,
    MODE_STATIC: 0x01,
    MODE_BREATHING: 0x02,
    MODE_RAINBOW: 0x03,
    MODE_WAVE: 0x04,
}


@dataclass
class RGBProfile:
    mode: str = MODE_RAINBOW
    speed: int = 3          # 1 (slow) .. 5 (fast)
    brightness: int = 4     # 1 (dim) .. 8 (max)
    color_r: int = 0
    color_g: int = 255
    color_b: int = 0


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


class RGBHIDController:
    """
    Discovers the ITE/Uniwill RGB HID device, verifies a working report ID,
    then sends 64-byte feature reports to apply lighting profiles.
    """

    def __init__(self, emit_log: Callable[[str], None] = lambda m: None) -> None:
        self._log = emit_log
        self._device_path: Optional[str] = None
        self._report_id: int = 0
        self._handle = None
        self._hid_dll = None
        self._verified = False

    # ── Windows HID API bindings ────────────────────────────────
    def _load_api(self) -> bool:
        try:
            self._hid_dll = ctypes.WinDLL("hid.dll")
            self._setup_dll = ctypes.WinDLL("setupapi.dll")
            return True
        except OSError as exc:
            self._log("[RGB] HID DLL load failed: %s" % exc)
            return False

    def _enumerate_paths(self) -> List[str]:
        """Enumerate HID device interface paths via SetupDi API."""
        guid = ctypes.c_char_p(b"{4d1e55b2-f16f-11cf-88cb-001111000030}")
        DIGCF_PRESENT = 0x02
        DIGCF_DEVICEINTERFACE = 0x10
        paths: List[str] = []
        setup = ctypes.WinDLL("setupapi")
        class_guid = ctypes.c_char_p(b"{4d1e55b2-f16f-11cf-88cb-001111000030}")
        handle = setup.SetupDiGetClassDevsW(
            class_guid, None, None, DIGCF_PRESENT | DIGCF_DEVICEINTERFACE)
        if handle == ctypes.c_void_p(-1).value or not handle:
            self._log("[RGB] SetupDiGetClassDevs failed.")
            return paths
        try:
            index = 0
            while True:
                # SP_DEVICE_INTERFACE_DATA (fixed size 28 bytes on x64)
                data = (ctypes.c_char * 28)()
                ctypes.memset(data, 0, 28)
                data[0] = 28  # cbSize
                if not setup.SetupDiEnumDeviceInterfaces(handle, None, class_guid, index, data):
                    break
                # Query required buffer size
                size = wintypes.DWORD(0)
                setup.SetupDiGetDeviceInterfaceDetailW(handle, data, None, 0, ctypes.byref(size), None)
                if size.value == 0:
                    index += 1
                    continue
                buf = ctypes.create_string_buffer(size.value)
                detail = ctypes.create_string_buffer(size.value)
                # SP_DEVICE_INTERFACE_DETAIL_DATA_W: cbSize DWORD then TCHAR path
                ctypes.memset(buf, 0, size.value)
                buf[0:4] = (8).to_bytes(4, "little")  # cbSize on x64
                if setup.SetupDiGetDeviceInterfaceDetailW(handle, data, buf, size, None, None):
                    # Path begins at offset 4 (after cbSize)
                    raw = buf.raw[4:size.value]
                    path = raw.decode("utf-16-le", errors="ignore").split("\x00")[0]
                    if path:
                        paths.append(path)
                index += 1
        finally:
            setup.SetupDiDestroyDeviceInfoList(handle)
        self._log("[RGB] Enumerated %d HID collection node(s)" % len(paths))
        return paths

    def _vid_pid_from_path(self, path: str) -> Optional[tuple]:
        upper = path.upper()
        if "VID_" not in upper or "PID_" not in upper:
            return None
        try:
            vid = int(upper.split("VID_")[1][:4], 16)
            pid = int(upper.split("PID_")[1][:4], 16)
            return (vid, pid)
        except (ValueError, IndexError):
            return None

    # ── open/write/close ────────────────────────────────────────
    def _open(self, path: str):
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.CreateFileW(
            path,
            GENERIC_READ | GENERIC_WRITE,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            None,
            OPEN_EXISTING,
            0,
            None,
        )
        if handle in (None, -1) or handle == ctypes.c_void_p(-1).value:
            return None
        return handle

    def _write_feature_report(self, handle, report_id: int, payload: bytes) -> bool:
        hid = ctypes.WinDLL("hid.dll")
        buf = (ctypes.c_ubyte * 65)()
        buf[0] = report_id
        for i, b in enumerate(payload[:64]):
            buf[i + 1] = b
        written = wintypes.BOOL(False)
        res = hid.HidD_SetFeature(handle, buf, ctypes.sizeof(buf))
        if not res:
            err = ctypes.get_last_error()
            self._log("[RGB] HidD_SetFeature failed (report_id=0x%02X, gle=%d)" % (report_id, err))
            return False
        return True

    def _read_feature_report(self, handle, report_id: int) -> Optional[bytes]:
        hid = ctypes.WinDLL("hid.dll")
        buf = (ctypes.c_ubyte * 65)()
        buf[0] = report_id
        if not hid.HidD_GetFeature(handle, buf, ctypes.sizeof(buf)):
            return None
        return bytes(buf[0:65])

    # ── discovery ────────────────────────────────────────────────
    def detect(self) -> bool:
        """Find a known RGB controller and verify a responsive report ID."""
        paths = self._enumerate_paths()
        for path in paths:
            vid_pid = self._vid_pid_from_path(path)
            if not vid_pid or vid_pid not in RGB_CONTROLLER_IDS:
                continue
            name = RGB_CONTROLLER_IDS[vid_pid]
            self._log("[RGB] Candidate found: %s VID=0x%04X PID=0x%04X" % (name, vid_pid[0], vid_pid[1]))
            handle = self._open(path)
            if handle is None:
                self._log("[RGB] Open failed (insufficient privileges?) - skipping")
                continue
            try:
                # Probe each candidate report ID with a harmless query
                for rid in CANDIDATE_REPORT_IDS:
                    query = bytes([0xA1]) + bytes(62)
                    ok = self._write_feature_report(handle, rid, query)
                    if not ok:
                        continue
                    resp = self._read_feature_report(handle, rid)
                    if resp is not None and any(b != 0 for b in resp[1:8]):
                        self._device_path = path
                        self._report_id = rid
                        self._handle = handle
                        self._verified = True
                        self._log("[RGB] RGB controller verified: %s report_id=0x%02X" % (name, rid))
                        return True
            finally:
                if handle is not None and not self._verified:
                    ctypes.WinDLL("kernel32").CloseHandle(handle)
        self._log("[RGB] No ITE 8291 / Uniwill RGB controller responded.")
        return False

    # ── profile application ─────────────────────────────────────
    def apply_profile(self, profile: RGBProfile) -> bool:
        """Send a 64-byte feature report applying the requested lighting mode."""
        if not self._verified:
            self._log("[RGB] Controller not verified - call detect() first.")
            return False
        mode_code = MODE_CODES.get(profile.mode)
        if mode_code is None:
            raise ValueError("Unknown RGB mode: %s" % profile.mode)
        payload = bytearray(64)
        payload[0] = 0x51                          # Apply-profile command byte
        payload[1] = mode_code
        payload[2] = _clamp(profile.speed, 1, 5)
        payload[3] = _clamp(profile.brightness, 1, 8)
        payload[4] = _clamp(profile.color_r, 0, 255)
        payload[5] = _clamp(profile.color_g, 0, 255)
        payload[6] = _clamp(profile.color_b, 0, 255)
        ok = self._write_feature_report(self._handle, self._report_id, bytes(payload))
        if ok:
            self._log("[RGB] Profile applied: mode=%s speed=%d brightness=%d rgb=(%d,%d,%d)"
                      % (profile.mode, profile.speed, profile.brightness,
                         profile.color_r, profile.color_g, profile.color_b))
        return ok

    def turn_off(self) -> bool:
        return self.apply_profile(RGBProfile(mode=MODE_OFF))

    def close(self) -> None:
        if self._handle is not None:
            try:
                ctypes.WinDLL("kernel32").CloseHandle(self._handle)
            except Exception:
                pass
            self._handle = None
        self._verified = False


if __name__ == "__main__":
    print("ASLAY RGB HID self-test")
    print("=" * 40)
    rgb = RGBHIDController(emit_log=print)
    if rgb.detect():
        print("Detected. Applying rainbow for 5 seconds...")
        rgb.apply_profile(RGBProfile(mode=MODE_RAINBOW, speed=3, brightness=4))
        time.sleep(5)
        print("Switching to static green for 5 seconds...")
        rgb.apply_profile(RGBProfile(mode=MODE_STATIC, color_r=0, color_g=255, color_b=0))
        time.sleep(5)
        print("Turning off...")
        rgb.turn_off()
        rgb.close()
    else:
        print("No compatible RGB controller detected on this machine.")
    print("Self-test complete.")
