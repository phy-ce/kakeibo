"""Determines the base folder depending on the runtime environment (source vs PyInstaller exe)."""
import os
import sys


def app_dir() -> str:
    """Base folder where DB / uploads / secret files are located.

    - Source execution: repo root (one level above the kakeibo/ package)
    - PyInstaller --onefile exe: the folder containing the exe
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    # kakeibo/paths.py -> two levels up = repo root
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
