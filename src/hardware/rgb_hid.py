# -*- coding: utf-8 -*-
"""
ASLAY Control Hub - RGB HID Controller
Keyboard backlight control for ITE 8291 / Uniwill RGB controllers via
USB HID feature reports (64-byte). Detection probes known VID/PID pairs.

Self-test:  python -m src.hardware.rgb_hid   (requires Administrator)
"""
from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from dataclasses import dataclass
from typing import Callable, List, Optional

# Known RGB controller USB VID/PID pairs (probed at runtime)
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
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

DIGCF_PRESENT = 0x02
DIGCF_DEVICEINTERFACE = 0x10

# Report IDs commonly used by the ITE 8291 family (probed in order)
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
    """Discovers the ITE/Uniwill RGB HID device and applies lighting."""

    def __init__(self, emit_log: Callable[[str], None] = lambda m: None) -> None:
        self._log = emit_log
        self._device_path: Optional[str] = None
        self._report_id: int = 0
        self._handle = None
        self._verified = False
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._hid = ctypes.WinDLL("hid.dll")
        self._setup = ctypes.WinDLL("setupapi")

    # ── HID GUID (queried from hid.dll, never hardcoded bytes) ──
    def _hid_guid(self) -> ctypes.c_char * 16:
        buf = (ctypes.c_char * 16)()
        self._hid.HidD_GetHidGuid(ctypes.byref(buf))
        return buf

    # ── enumeration ─────────────────────────────────────────────
    def _enumerate_paths(self) -> List[str]:
        paths: List[str] = []
        guid = self._hid_guid()

        class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("InterfaceClassGuid", (ctypes.c_char * 16)),
                ("Flags", wintypes.DWORD),
                ("Reserved", ctypes.c_void_p),
            ]

        data = SP_DEVICE_INTERFACE_DATA()
        data.cbSize = ctypes.sizeof(data)

        handle = self._setup.SetupDiGetClassDevsW(
            ctypes.byref(guid), None, None,
            DIGCF_PRESENT | DIGCF_DEVICEINTERFACE)
        if not handle or handle == INVALID_HANDLE_VALUE:
            self._log("[RGB] SetupDiGetClassDevs failed.")
            return paths
        try:
            index = 0
            while True:
                if not self._setup.SetupDiEnumDeviceInterfaces(
                        handle, None, ctypes.byref(guid), index,
                        ctypes.byref(data)):
                    break
                required = wintypes.DWORD(0)
                self._setup.SetupDiGetDeviceInterfaceDetailW(
                    handle, ctypes.byref(data), None, 0,
                    ctypes.byref(required), None)
                if required.value == 0:
                    index += 1
                    continue
                buf = ctypes.create_string_buffer(required.value)
                # SP_DEVICE_INTERFACE_DETAIL_DATA_W: cbSize then null-terminated path
                # cbSize is 8 on x64 (4 header + 4 padding), 6 on x86.
                ctypes.memset(buf, 0, required.value)
                struct.pack_into("I", buf, 0,
                                 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6)
                if self._setup.SetupDiGetDeviceInterfaceDetailW(
                        handle, ctypes.byref(data), buf, required.value,
                        None, None):
                    raw = buf.raw
                    # Path starts right after the cbSize field (offset 4)
                    wide = raw[4:].split(b"\x00\x00")[0]
                    try:
                        path = wide.decode("utf-16-le", errors="ignore")
                        if path:
                            paths.append(path)
                    except Exception:
                        pass
                index += 1
        finally:
            self._setup.SetupDiDestroyDeviceInfoList(handle)
        self._log("[RGB] Enumerated %d HID collection node(s)." % len(paths))
        return paths

    @staticmethod
    def _vid_pid_from_path(path: str) -> Optional[tuple]:
        upper = path.upper()
        if "VID_" not in upper or "PID_" not in upper:
            return None
        try:
            vid = int(upper.split("VID_")[1][:4], 16)
            pid = int(upper.split("PID_")[1][:4], 16)
            return (vid, pid)
        except (ValueError, IndexError):
            return None

    # ── open / read / write ────────────────────────────────────
    def _open(self, path: str):
        handle = self._kernel32.CreateFileW(
            path, GENERIC_READ | GENERIC_WRITE,
            FILE_SHARE_READ | FILE_SHARE_WRITE, None,
            OPEN_EXISTING, 0, None)
        if not handle or handle == INVALID_HANDLE_VALUE:
            return None
        return handle

    def _write_feature_report(self, handle, report_id: int, payload: bytes) -> bool:
        buf = (ctypes.c_ubyte * 65)()
        buf[0] = report_id
        for i, b in enumerate(payload[:64]):
            buf[i + 1] = b
        if not self._hid.HidD_SetFeature(
                wintypes.HANDLE(handle), buf, ctypes.sizeof(buf)):
            self._log("[RGB] HidD_SetFeature failed (report_id=0x%02X, gle=%d)."
                      % (report_id, ctypes.get_last_error()))
            return False
        return True

    def _read_feature_report(self, handle, report_id: int) -> Optional[bytes]:
        buf = (ctypes.c_ubyte * 65)()
        buf[0] = report_id
        if not self._hid.HidD_GetFeature(
                wintypes.HANDLE(handle), buf, ctypes.sizeof(buf)):
            return None
        return bytes(buf)

    # ── discovery with verification ─────────────────────────────
    def detect(self) -> bool:
        for path in self._enumerate_paths():
            vid_pid = self._vid_pid_from_path(path)
            if not vid_pid or vid_pid not in RGB_CONTROLLER_IDS:
                continue
            name = RGB_CONTROLLER_IDS[vid_pid]
            self._log("[RGB] Candidate: %s VID=0x%04X PID=0x%04X"
                      % (name, vid_pid[0], vid_pid[1]))
            handle = self._open(path)
            if handle is None:
                self._log("[RGB] Open failed (run as Administrator) - skip.")
                continue
            try:
                for rid in CANDIDATE_REPORT_IDS:
                    query = bytes([0xA1]) + bytes(62)
                    if not self._write_feature_report(handle, rid, query):
                        continue
                    resp = self._read_feature_report(handle, rid)
                    if resp is not None and any(b != 0 for b in resp[1:8]):
                        self._device_path = path
                        self._report_id = rid
                        self._handle = handle
                        self._verified = True
                        self._log("[RGB] Verified controller: %s (report 0x%02X)"
                                  % (name, rid))
                        return True
            except Exception:
                self._kernel32.CloseHandle(wintypes.HANDLE(handle))
        self._log("[RGB] No ITE 8291 / Uniwill RGB controller responded.")
        return False

    # ── profile application ─────────────────────────────────────
    def apply_profile(self, profile: RGBProfile) -> bool:
        if not self._verified:
            self._log("[RGB] Controller not verified - run detect() first.")
            return False
        mode_code = MODE_CODES.get(profile.mode)
        if mode_code is None:
            raise ValueError("Unknown RGB mode: %s" % profile.mode)
        payload = bytearray(64)
        payload[0] = 0x51
        payload[1] = mode_code
        payload[2] = _clamp(profile.speed, 1, 5)
        payload[3] = _clamp(profile.brightness, 1, 8)
        payload[4] = _clamp(profile.color_r, 0, 255)
        payload[5] = _clamp(profile.color_g, 0, 255)
        payload[6] = _clamp(profile.color_b, 0, 255)
        ok = self._write_feature_report(self._handle, self._report_id, bytes(payload))
        if ok:
            self._log("[RGB] Applied: mode=%s speed=%d brightness=%d rgb=(%d,%d,%d)"
                      % (profile.mode, profile.speed, profile.brightness,
                         profile.color_r, profile.color_g, profile.color_b))
        return ok

    def turn_off(self) -> bool:
        return self.apply_profile(RGBProfile(mode=MODE_OFF))

    def close(self) -> None:
        if self._handle is not None:
            try:
                self._kernel32.CloseHandle(wintypes.HANDLE(self._handle))
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
