import ctypes
import os
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event

from .parallel import _parallel

MAX_FILE_BYTES = 64 * 1024
MAX_TOTAL_BYTES = 32 * 1024**2
MAX_FILES = 4096
MIN_FILES = 64
MAX_WORKERS = 16
MIN_READ_SECONDS = 0.001
DRIVE_FIXED = 3
GENERIC_READ = 0x80000000
FILE_WRITE_ATTRIBUTES = 0x0100
FILE_SHARE_READ = 0x01
FILE_SHARE_WRITE = 0x02
FILE_SHARE_DELETE = 0x04
OPEN_EXISTING = 3
FILE_FLAG_SEQUENTIAL_SCAN = 0x08000000
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000


class _WindowsReader:
    def __init__(self) -> None:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self._drive_type = kernel.GetDriveTypeW
        self._drive_type.argtypes = [wintypes.LPCWSTR]
        self._drive_type.restype = wintypes.UINT
        self._open = kernel.CreateFileW
        self._open.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        self._open.restype = wintypes.HANDLE
        self._set_time = kernel.SetFileTime
        self._set_time.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        ]
        self._set_time.restype = wintypes.BOOL
        self._read = kernel.ReadFile
        self._read.argtypes = [
            wintypes.HANDLE,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPVOID,
        ]
        self._read.restype = wintypes.BOOL
        self._close = kernel.CloseHandle
        self._close.argtypes = [wintypes.HANDLE]
        self._close.restype = wintypes.BOOL

    def local(self, anchor: str) -> bool:
        return bool(self._drive_type(anchor) == DRIVE_FIXED)

    def read(self, path: Path, cancel: Event) -> None:
        if cancel.is_set():
            return
        handle = self._open(
            str(path),
            GENERIC_READ | FILE_WRITE_ATTRIBUTES,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            None,
            OPEN_EXISTING,
            FILE_FLAG_SEQUENTIAL_SCAN | FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        if handle == ctypes.c_void_p(-1).value:
            return
        try:
            # Suppress access-time updates for this read handle without changing stored timestamps.
            frozen = wintypes.FILETIME(0xFFFFFFFF, 0xFFFFFFFF)
            if self._set_time(handle, None, ctypes.byref(frozen), None) and not cancel.is_set():
                buffer = ctypes.create_string_buffer(MAX_FILE_BYTES)
                read = wintypes.DWORD()
                self._read(handle, buffer, MAX_FILE_BYTES, ctypes.byref(read), None)
        finally:
            self._close(handle)


@dataclass
class PrefetchPlan:
    files: list[Path] = field(default_factory=list)
    _bytes: int = 0

    def add(self, path: Path | str, info: os.stat_result) -> None:
        if (
            0 < info.st_size <= MAX_FILE_BYTES
            and self._bytes + info.st_size <= MAX_TOTAL_BYTES
            and len(self.files) < MAX_FILES
        ):
            self.files.append(Path(path))
            self._bytes += info.st_size

    def warm(self) -> None:
        workers = min(MAX_WORKERS, os.cpu_count() or 1)
        if sys.platform != "win32" or len(self.files) < MIN_FILES or workers == 1:
            return
        reader = _WindowsReader()
        local = {anchor: reader.local(anchor) for anchor in {path.anchor for path in self.files}}
        files = [path for path in self.files if local[path.anchor]]
        if len(files) >= MIN_FILES:
            samples = files[:: max(1, len(files) // 4)][:4]
            cancel = Event()
            start = time.perf_counter()
            for path in samples:
                reader.read(path, cancel)
            if (time.perf_counter() - start) / len(samples) >= MIN_READ_SECONDS:
                sampled = set(samples)
                _parallel((path for path in files if path not in sampled), reader.read, workers)
