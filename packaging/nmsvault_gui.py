"""PyInstaller entry point for the windowed app.

Kept as a tiny standalone script (rather than ``-m nms_save_vault.gui``) because
PyInstaller analyses a real file. All it does is hand off to the GUI's ``main``.
"""
from nms_save_vault.gui import main

if __name__ == "__main__":
    raise SystemExit(main())
