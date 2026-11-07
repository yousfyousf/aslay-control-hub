# ASLAY Control Hub - build instructions note

If you cloned this repository and want to build the executable locally:

1. Install Python 3.11 for Windows.
2. Open a terminal (as Administrator) in this folder.
3. Run: python -m venv .venv && .venv\Scripts\activate
4. Run: pip install -r requirements.txt pyinstaller
5. Run: pyinstaller --noconfirm --onefile --windowed --name ASLAY_Control_Hub --manifest app.manifest main.py
6. The executable appears in: dist\ASLAY_Control_Hub.exe
7. Place WinRing0x64.dll next to the exe and run it as Administrator.

See README.md for full hardware access requirements.
