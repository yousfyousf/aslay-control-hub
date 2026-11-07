# -*- coding: utf-8 -*-
"""
ASLAY Control Hub - main window
PyQt6 UI: sidebar navigation, Dark/Light theme, Arabic RTL flip (Cairo font).
Pages wire the real backend modules: GPU / Battery / RGB / Storage / Wi-Fi /
Drivers / BIOS / Processes.

Run:  python -m src.ui.main_window   (requires Administrator for full features)
"""
from __future__ import annotations

import sys
from typing import Callable, Optional

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QHBoxLayout, QLabel, QMainWindow, QPushButton,
    QStackedWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from src.hardware.ec_driver import ECDriver
from src.hardware.gpu_tuner import GPUTuner
from src.services.bios_checker import BiosChecker
from src.services.driver_manager import DriverManager
from src.services.storage_scanner import StorageScanner
from src.services.task_manager import ProcessManager
from src.services.wifi_booster import WiFiBooster

DARK_BG = "#12161c"
DARK_PANEL = "#1a2029"
DARK_TEXT = "#e6e9ee"
ACCENT = "#3fc1ff"


class Page(QWidget):
    """Base page: title, info label, action log."""

    def __init__(self, title: str) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        title_label = QLabel(title)
        title_label.setObjectName("pageTitle")
        layout.addWidget(title_label)
        self.info = QLabel("-")
        self.info.setWordWrap(True)
        layout.addWidget(self.info)
        self.log_lines: list = []
        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        layout.addStretch()

    def log(self, message: str) -> None:
        self.log_lines.append(message)
        if len(self.log_lines) > 60:
            self.log_lines = self.log_lines[-60:]
        self.status.setText("\n".join(self.log_lines[-12:]))


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("ASLAY Control Hub")
        self.resize(1100, 720)
        self._dark = True

        # ── backends ─────────────────────────────────────────────
        self.gpu_tuner = GPUTuner(log=lambda m: self._page_log("GPU", m))
        self.ec = ECDriver(log=lambda m: self._page_log("Battery", m))
        self.wifi = WiFiBooster(log=lambda m: self._page_log("Wi-Fi", m))
        self.drivers = DriverManager(log=lambda m: self._page_log("Drivers", m))
        self.storage = StorageScanner(log=lambda m: self._page_log("Storage", m))
        self.bios = BiosChecker(log=lambda m: self._page_log("BIOS", m))
        self.procman = ProcessManager(log=lambda m: self._page_log("Processes", m))

        self._build_ui()
        self._apply_theme(True)
        QTimer.singleShot(200, self._init_backends)

    # ── log routing (backends log before pages exist) ────────────
    def _page_log(self, page: str, message: str) -> None:
        if hasattr(self, "pages"):
            widget = self.pages.widget(self._page_index(page))
            if widget:
                widget.log(message)

    def _page_index(self, name: str) -> int:
        names = ["GPU", "Battery", "RGB", "Storage", "Wi-Fi", "Drivers", "BIOS", "Processes"]
        return names.index(name) if name in names else 0

    # ── UI construction ─────────────────────────────────────────
    def _build_ui(self) -> None:
        central = QWidget()
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        sidebar = QVBoxLayout()
        sidebar.setContentsMargins(8, 8, 8, 8)
        self.pages = QStackedWidget()

        # GPU page with buttons
        gpu_page = Page("GPU Tuner")
        gpu_boost_btn = QPushButton("Enable Game Boost (max TGP + safe offsets)")
        gpu_boost_btn.clicked.connect(self._gpu_boost)
        gpu_reset_btn = QPushButton("Reset offsets to factory")
        gpu_reset_btn.clicked.connect(self._gpu_reset)
        gpu_page.layout().insertWidget(2, gpu_boost_btn)
        gpu_page.layout().insertWidget(3, gpu_reset_btn)

        # Battery page
        battery_page = Page("Battery Charge Limiter")
        battery_page.layout().insertWidget(2, QLabel("Charge ceiling:"))
        self.battery_combo = QComboBox()
        self.battery_combo.addItems(["60%", "80%", "100% (cancel limit)"])
        battery_page.layout().insertWidget(3, self.battery_combo)
        battery_btn = QPushButton("Apply charge limit")
        battery_btn.clicked.connect(self._battery_apply)
        battery_page.layout().insertWidget(4, battery_btn)
        battery_refresh = QPushButton("Refresh battery status")
        battery_refresh.clicked.connect(self._battery_refresh)
        battery_page.layout().insertWidget(5, battery_refresh)

        # RGB page
        rgb_page = Page("RGB Keyboard")
        rgb_combo = QComboBox()
        rgb_combo.addItems(["Rainbow", "Static (custom color)", "Breathing", "Wave", "Off"])
        rgb_page.layout().insertWidget(2, rgb_combo)
        rgb_btn = QPushButton("Apply lighting")
        rgb_btn.clicked.connect(self._rgb_apply)
        rgb_page.layout().insertWidget(3, rgb_btn)

        # Storage page
        storage_page = Page("Storage / S.M.A.R.T")
        storage_btn = QPushButton("Scan all disks")
        storage_btn.clicked.connect(self._storage_scan)
        storage_page.layout().insertWidget(2, storage_btn)

        # Wi-Fi page
        wifi_page = Page("Wi-Fi Booster")
        wifi_btn = QPushButton("Boost Wi-Fi adapter")
        wifi_btn.clicked.connect(self._wifi_boost)
        wifi_restore_btn = QPushButton("Restore defaults")
        wifi_restore_btn.clicked.connect(self._wifi_restore)
        wifi_page.layout().insertWidget(2, wifi_btn)
        wifi_page.layout().insertWidget(3, wifi_restore_btn)

        # Drivers page
        drivers_page = Page("Driver Manager")
        drivers_btn = QPushButton("Scan for problem devices (Code 10/28/43...)")
        drivers_btn.clicked.connect(self._drivers_scan)
        drivers_page.layout().insertWidget(2, drivers_btn)

        # BIOS page
        bios_page = Page("BIOS Checker")
        bios_btn = QPushButton("Read BIOS info + Secure Boot + TPM")
        bios_btn.clicked.connect(self._bios_read)
        bios_page.layout().insertWidget(2, bios_btn)

        # Processes page
        proc_page = Page("Process Manager")
        self.proc_table = QTableWidget(0, 5)
        self.proc_table.setHorizontalHeaderLabels(["Name", "PID", "CPU %", "RAM MB", "Protected"])
        proc_page.layout().insertWidget(2, self.proc_table)
        proc_refresh = QPushButton("Refresh processes (top 30 by CPU)")
        proc_refresh.clicked.connect(self._proc_refresh)
        proc_page.layout().insertWidget(3, proc_refresh)

        nav_items = [
            ("GPU", gpu_page),
            ("Battery", battery_page),
            ("RGB", rgb_page),
            ("Storage", storage_page),
            ("Wi-Fi", wifi_page),
            ("Drivers", drivers_page),
            ("BIOS", bios_page),
            ("Processes", proc_page),
        ]

        self.buttons = []
        for i, (label, page) in enumerate(nav_items):
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda checked, idx=i: self._switch(idx))
            sidebar.addWidget(btn)
            self.buttons.append(btn)
            self.pages.addWidget(page)

        theme_btn = QPushButton("Dark / Light")
        theme_btn.clicked.connect(self._toggle_theme)
        sidebar.addWidget(theme_btn)
        rtl_btn = QPushButton("Arabic RTL")
        rtl_btn.clicked.connect(self._flip_rtl)
        sidebar.addWidget(rtl_btn)
        sidebar.addStretch()

        sidebar_widget = QWidget()
        sidebar_widget.setObjectName("sidebar")
        sidebar_widget.setLayout(sidebar)
        sidebar_widget.setFixedWidth(200)
        root.addWidget(sidebar_widget)
        root.addWidget(self.pages)
        self.setCentralWidget(central)
        self._switch(0)

    def _switch(self, index: int) -> None:
        for i, btn in enumerate(self.buttons):
            btn.setChecked(i == index)
        self.pages.setCurrentIndex(index)

    # ── theme ───────────────────────────────────────────────────
    def _apply_theme(self, dark: bool) -> None:
        bg = DARK_BG if dark else "#f3f5f7"
        panel = DARK_PANEL if dark else "#ffffff"
        text = DARK_TEXT if dark else "#1c2430"
        self.setStyleSheet(
            "QMainWindow, QWidget { background: %s; color: %s; font-size: 14px; }"
            "QLabel#pageTitle { font-size: 20px; font-weight: 700; padding: 4px 0 10px 0; }"
            "#sidebar { background: %s; border-right: 1px solid #2a313c; }"
            "QPushButton { background: %s; color: %s; border: 1px solid #2a313c;"
            "  padding: 8px 12px; border-radius: 6px; text-align: left; }"
            "QPushButton:checked { background: %s; color: #06121b; font-weight: 600; }"
            "QTableWidget { gridline-color: #2a313c; }"
            "QComboBox { background: %s; color: %s; padding: 6px; border: 1px solid #2a313c; }"
            % (bg, text, panel, panel, text, ACCENT, panel, text))

    def _toggle_theme(self) -> None:
        self._dark = not self._dark
        self._apply_theme(self._dark)

    def _flip_rtl(self) -> None:
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        app = QApplication.instance()
        app.setFont(QFont("Cairo", 10))

    # ── backend init ────────────────────────────────────────────
    def _init_backends(self) -> None:
        self.gpu_tuner.start()
        self.ec.start()
        gpus = self.gpu_tuner.detect_physical_gpus()
        if gpus:
            self._page("GPU").info.setText("GPU: %s (%d MB VRAM, driver %s)"
                                            % (gpus[0]["name"], gpus[0]["vram_mb"],
                                               gpus[0]["driver_version"]))
        else:
            self._page("GPU").info.setText("No NVIDIA GPU via NVML (driver missing or AMD machine).")
        self._battery_refresh()

    def _page(self, name: str) -> Page:
        return self.pages.widget(self._page_index(name))

    # ── actions ──────────────────────────────────────────────────
    def _gpu_boost(self) -> None:
        page = self._page("GPU")
        devices = self.gpu_tuner.list_devices()
        if not devices:
            page.info.setText("No tunable NVIDIA GPU detected.")
            return
        ok = self.gpu_tuner.enable_game_boost(devices[0])
        page.info.setText("Game boost: %s" % ("applied" if ok else "rejected by driver"))

    def _gpu_reset(self) -> None:
        page = self._page("GPU")
        devices = self.gpu_tuner.list_devices()
        if not devices:
            page.info.setText("No tunable NVIDIA GPU detected.")
            return
        ok = self.gpu_tuner.reset_offsets(devices[0])
        page.info.setText("Offsets reset: %s" % ("done" if ok else "failed"))

    def _battery_apply(self) -> None:
        page = self._page("Battery")
        choice = self.battery_combo.currentText()
        percent = int(choice.split("%")[0])
        ok = self.ec.set_charge_limit(percent)
        if ok:
            page.info.setText("Charge limit applied: %d%%" % percent)
        else:
            page.info.setText("Could not apply limit (need Administrator + WinRing0x64.dll).")

    def _battery_refresh(self) -> None:
        page = self._page("Battery") if hasattr(self, "pages") else None
        if page is None:
            return
        st = self.ec.read_limits_status()
        if st.battery_present:
            page.info.setText(
                "Design: %d mWh | Full: %d mWh | Cycles: %d | EC write: %s (%s)"
                % (st.design_capacity_mwh, st.full_charge_mwh, st.cycle_count,
                   "available" if st.ec_writable else "unavailable", st.source))

    def _rgb_apply(self) -> None:
        from src.hardware.rgb_hid import RGBHIDController, RGBProfile, MODE_CODES
        page = self._page("RGB")
        mode_key = {
            "Rainbow": "rainbow",
            "Static (custom color)": "static",
            "Breathing": "breathing",
            "Wave": "wave",
            "Off": "off",
        }[self.sender().parent().findChild(QComboBox).currentText()]
        controller = RGBHIDController(emit_log=page.log)
        if controller.detect():
            ok = controller.apply_profile(RGBProfile(mode=mode_key))
            page.info.setText("RGB: %s" % ("applied" if ok else "failed"))
            controller.close()
        else:
            page.info.setText("No compatible RGB controller found (run as Administrator).")

    def _storage_scan(self) -> None:
        page = self._page("Storage")
        page.info.setText("Scanning disks...")
        QApplication.processEvents()
        disks = self.storage.scan_all()
        if not disks:
            page.info.setText("No physical disks found.")
            return
        lines = []
        for d in disks:
            line = "%s - %s - %.1f GB - Temp: %s C - S.M.A.R.T: %s" % (
                d.model, d.media_type, d.size_gb,
                d.temperature_c if d.temperature_c is not None else "n/a",
                "OK" if d.smart_status_ok else "PROBLEM")
            lines.append(line)
            for w in d.warnings:
                lines.append("  ! " + w)
        page.info.setText("\n".join(lines))

    def _wifi_boost(self) -> None:
        page = self._page("Wi-Fi")
        adapters = self.wifi.detect_adapters()
        if not adapters:
            page.info.setText("No wireless PCI adapter found.")
            return
        result = self.wifi.apply_boost(adapters[0])
        page.info.setText("Boost: %d applied, %d failed. (%s)"
                          % (len(result.applied), len(result.failed),
                             "; ".join(result.failed) if result.failed else "all OK"))

    def _wifi_restore(self) -> None:
        page = self._page("Wi-Fi")
        adapters = self.wifi.detect_adapters()
        if not adapters:
            page.info.setText("No wireless PCI adapter found.")
            return
        result = self.wifi.restore_default(adapters[0])
        page.info.setText("Restore: %d applied, %d failed."
                          % (len(result.applied), len(result.failed)))

    def _drivers_scan(self) -> None:
        page = self._page("Drivers")
        problems = self.drivers.scan_problem_devices()
        if not problems:
            page.info.setText("All PnP devices healthy (no problem codes).")
        else:
            lines = ["%d device(s) need attention:" % len(problems)]
            for p in problems:
                lines.append(" - %s [Code %d] %s" % (p.device_name, p.problem_code, p.problem_text))
            page.info.setText("\n".join(lines))

    def _bios_read(self) -> None:
        page = self._page("BIOS")
        info = self.bios.read_info()
        page.info.setText(
            "%s %s (%s)\nBoard: %s %s | Secure Boot: %s | TPM: %s\n%s"
            % (info.vendor, info.version, info.release_date,
               info.board_vendor, info.board_model,
               "ON" if info.secure_boot_enabled else "OFF",
               "present" if info.tpm_present else "not found",
               "\n".join(info.notes) if info.notes else "No notes."))

    def _proc_refresh(self) -> None:
        page = self._page("Processes")
        snap = self.procman.snapshot(interval=1.0)
        top = snap.processes[:30]
        self.proc_table.setRowCount(len(top))
        for row, pr in enumerate(top):
            self.proc_table.setItem(row, 0, QTableWidgetItem(pr.name))
            self.proc_table.setItem(row, 1, QTableWidgetItem(str(pr.pid)))
            self.proc_table.setItem(row, 2, QTableWidgetItem("%.1f" % pr.cpu_percent))
            self.proc_table.setItem(row, 3, QTableWidgetItem("%.1f" % pr.ram_mb))
            self.proc_table.setItem(row, 4, QTableWidgetItem("yes" if pr.is_critical else "-"))
        page.info.setText("Total CPU: %.1f%% | Used RAM: %.0f MB | %d processes"
                          % (snap.total_cpu, snap.total_ram_mb, len(snap.processes)))

    # ── cleanup ─────────────────────────────────────────────────
    def closeEvent(self, event) -> None:
        self.ec.stop()
        self.gpu_tuner.stop()
        self.procman.close()
        event.accept()


def main() -> int:
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
