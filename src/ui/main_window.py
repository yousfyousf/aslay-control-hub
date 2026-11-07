# -*- coding: utf-8 -*-
"""
ASLAY Control Hub - main window
PyQt6 UI: collapsible sidebar, Dark/Light themes, custom accent color,
full Arabic RTL flip (Cairo font), pages for GPU / Battery / RGB / Storage /
Wi-Fi / Drivers / BIOS / Processes.

Self-test:  python -m src.ui.main_window   (requires Administrator)
"""
from __future__ import annotations

import sys
from typing import Optional

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QMainWindow, QPushButton,
    QStackedWidget, QVBoxLayout, QWidget,
)

from src.hardware.gpu_tuner import GPUTuner
from src.hardware.ec_driver import ECDriver

DARK_BG = "#12161c"
DARK_PANEL = "#1a2029"
DARK_TEXT = "#e6e9ee"
ACCENT = "#3fc1ff"


class SidebarButton(QPushButton):
    def __init__(self, label: str, on_click) -> None:
        super().__init__(label)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clicked.connect(on_click)


class PlaceholderPage(QWidget):
    def __init__(self, title: str) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        title_label = QLabel(title)
        title_label.setObjectName("pageTitle")
        layout.addWidget(title_label)
        self.info = QLabel("-")
        self.info.setWordWrap(True)
        layout.addWidget(self.info)
        layout.addStretch()


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("ASLAY Control Hub")
        self.resize(1100, 700)
        self.gpu_tuner: Optional[GPUTuner] = None
        self.ec: Optional[ECDriver] = None
        self._dark = True
        self._build_ui()
        self._apply_theme(True)
        QTimer.singleShot(300, self._lazy_init_backend)

    def _build_ui(self) -> None:
        central = QWidget()
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        sidebar = QVBoxLayout()
        sidebar.setContentsMargins(8, 8, 8, 8)
        self.pages = QStackedWidget()
        nav_items = [
            ("GPU", PlaceholderPage("GPU Tuner")),
            ("Battery", PlaceholderPage("Battery Charge Limiter")),
            ("RGB", PlaceholderPage("RGB Keyboard")),
            ("Storage", PlaceholderPage("Storage / SMART")),
            ("Wi-Fi", PlaceholderPage("Wi-Fi Booster")),
            ("Drivers", PlaceholderPage("Driver Manager")),
            ("BIOS", PlaceholderPage("BIOS Checker")),
            ("Processes", PlaceholderPage("Process Manager")),
        ]
        self.buttons = []
        for i, (label, page) in enumerate(nav_items):
            btn = SidebarButton(label, lambda checked, idx=i: self._switch(idx))
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
        sidebar_widget.setFixedWidth(190)
        root.addWidget(sidebar_widget)
        root.addWidget(self.pages)
        self.setCentralWidget(central)
        self._switch(0)

    def _switch(self, index: int) -> None:
        for i, btn in enumerate(self.buttons):
            btn.setChecked(i == index)
        self.pages.setCurrentIndex(index)

    def _apply_theme(self, dark: bool) -> None:
        bg = DARK_BG if dark else "#f3f5f7"
        panel = DARK_PANEL if dark else "#ffffff"
        text = DARK_TEXT if dark else "#1c2430"
        self.setStyleSheet(
            "QMainWindow, QWidget { background: %s; color: %s; font-size: 14px; }"
            "QLabel#pageTitle { font-size: 20px; font-weight: 700; padding: 12px; }"
            "#sidebar { background: %s; border-right: 1px solid #2a313c; }"
            "QPushButton { background: %s; color: %s; border: 1px solid #2a313c;"
            "  padding: 8px 12px; border-radius: 6px; text-align: left; }"
            "QPushButton:checked { background: %s; color: #06121b; font-weight: 600; }"
            % (bg, text, panel, panel, text, ACCENT))

    def _toggle_theme(self) -> None:
        self._dark = not self._dark
        self._apply_theme(self._dark)

    def _flip_rtl(self) -> None:
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        app = QApplication.instance()
        app.setFont(QFont("Cairo", 10))

    def _lazy_init_backend(self) -> None:
        gpu_page = self.pages.widget(0)
        self.gpu_tuner = GPUTuner(log=lambda m: gpu_page.info.setText(m))
        self.gpu_tuner.start()
        gpus = self.gpu_tuner.detect_physical_gpus()
        if gpus:
            gpu_page.info.setText("GPU: %s (%d MB VRAM)" % (gpus[0]["name"], gpus[0]["vram_mb"]))
        else:
            gpu_page.info.setText("No NVIDIA GPU via NVML. Detection still runs via PCI.")
        self.ec = ECDriver(log=lambda m: None)
        self.ec.start()

    def closeEvent(self, event) -> None:
        if self.ec:
            self.ec.stop()
        if self.gpu_tuner:
            self.gpu_tuner.stop()
        event.accept()


def main() -> int:
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
