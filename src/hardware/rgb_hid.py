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
    (0x048D, 0x5702): "ITE 8291 RGB",          # Most common Uniwill/ITE laptop RGB
    (0x048D, 0xCE00): "ITE 8291 RGB (alt)",
    (0x048D, 0x8297): "ITE 8297 RGB",
    (0x1A2C: 0x4455): "TongFang RGB",
    (0x195D: 0x7008): "Clevo TongFang RGB",
    (0x0B05: 0x1869): "ASUS ITE RGB",
}

HID_USAGE_PAGE_GENERIC = 0x01
HID_USAGE_KEYBOARD = 0x06

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
FILE_SHARE_READ = 1
FILE_SHARE_WRITE = 2

HIDP_STATUS_SUCCESS = 0x00110057

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
