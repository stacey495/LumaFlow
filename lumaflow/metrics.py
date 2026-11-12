import os
from pathlib import Path

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.GetProcessTimes.argtypes = (wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4))

    class Memory(ctypes.Structure):
        _fields_ = [
            ("size", wintypes.DWORD),
            ("faults", wintypes.DWORD),
            ("peak_rss", ctypes.c_size_t),
            ("rss", ctypes.c_size_t),
            ("peak_paged", ctypes.c_size_t),
            ("paged", ctypes.c_size_t),
            ("peak_nonpaged", ctypes.c_size_t),
            ("nonpaged", ctypes.c_size_t),
            ("pagefile", ctypes.c_size_t),
            ("peak_pagefile", ctypes.c_size_t),
        ]

    psapi.GetProcessMemoryInfo.argtypes = (wintypes.HANDLE, ctypes.POINTER(Memory), wintypes.DWORD)

    def sample(pid):
        handle = kernel.OpenProcess(0x1010, False, pid)
        if not handle:
            return None
        try:
            created, exited, system, user = (wintypes.FILETIME() for _ in range(4))
            if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(system), ctypes.byref(user)):
                return None
            cpu = sum((t.dwHighDateTime << 32) + t.dwLowDateTime for t in (system, user)) / 10_000_000
            memory = Memory()
            memory.size = ctypes.sizeof(memory)
            psapi.GetProcessMemoryInfo(handle, ctypes.byref(memory), memory.size)
            return cpu, memory.rss
        finally:
            kernel.CloseHandle(handle)
else:
    def sample(pid):
        try:
            fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            cpu = (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")
            return cpu, int(fields[21]) * os.sysconf("SC_PAGE_SIZE")
        except (OSError, ValueError, IndexError):
            return None
