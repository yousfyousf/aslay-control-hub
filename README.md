# ASLAY Control Hub

Unified low-level laptop hardware control suite for Windows 10/11 (64-bit).

## Modules

- Hardware Scanner - CPUID/PCI/SMBIOS dynamic hardware detection
- EC Driver - Battery charge limiter (60/80/100%) via EC ports 0x62/0x66
- RGB HID - ITE 8291/Uniwill keyboard RGB via 64-byte feature reports
- GPU Tuner - NVAPI/NVML core/memory offsets + TGP power limits
- Wi-Fi Booster - Adapter power/roaming/5GHz optimization
- Storage Scanner - S.M.A.R.T attributes + health estimation
- Driver Manager - PnP problem codes, pnputil backup/rollback
- BIOS Checker - SMBIOS info, Secure Boot, TPM state
- Process Manager - CPU/RAM/GPU monitoring, safe termination

## Requirements

- Windows 10/11 64-bit, Administrator privileges
- Python 3.10/3.11 (development)
- WinRing0x64.dll next to the exe for EC writes (from LibreHardwareMonitor)
- NVIDIA driver for GPU tuning (nvapi.dll + NVML)

## Run from source

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m src.ui.main_window
```

## Build

GitHub Actions builds the executable via PyInstaller on push to main and
attaches it to Releases.
