"""PyInstaller launcher: ``pyinstaller run_app.py`` → CoDriverStudio.exe."""
import sys

from codriver_studio.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
