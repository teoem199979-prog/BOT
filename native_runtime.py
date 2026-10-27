"""Load app-local Microsoft CRT DLLs for OCR when the system runtime is absent."""
import ctypes
import os
from pathlib import Path

_cookies = []
_handles = []


def prepare_ocr_runtime():
    if os.name != "nt" or _cookies:
        return
    directory = Path(__file__).resolve().parent / "work" / "native_runtime"
    if not directory.is_dir():
        return
    cookie = os.add_dll_directory(str(directory))
    handles = []
    # Retain handles/cookies for the lifetime of ONNX and pyclipper.
    try:
        for name in ("msvcp140.dll", "msvcp140_1.dll"):
            path = directory / name
            if path.is_file():
                handles.append(ctypes.WinDLL(str(path)))
    except OSError:
        cookie.close()
        raise
    _cookies.append(cookie)
    _handles.extend(handles)
