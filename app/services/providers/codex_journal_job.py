"""Owned Windows process group, assigned before any Journal input is sent.

This is cleanup, not a security sandbox. Assignment follows Popen: a launcher
must not start work/children before reading stdin. Live launcher qualification
must verify that assumption; the adapter remains unregistered until then.
"""
import ctypes
from ctypes import wintypes


class _Limits(ctypes.Structure):
    _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                ("flags", wintypes.DWORD), ("min_working_set", ctypes.c_size_t),
                ("max_working_set", ctypes.c_size_t), ("active_processes", wintypes.DWORD),
                ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                ("scheduling", wintypes.DWORD)]


class _Io(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]


class _Extended(ctypes.Structure):
    _fields_ = [("basic", _Limits), ("io", _Io), ("process_memory", ctypes.c_size_t),
                ("job_memory", ctypes.c_size_t), ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]


class WindowsJob:
    def __init__(self, process_handle: int) -> None:
        kernel = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        self._kernel = kernel
        self._handle = kernel.CreateJobObjectW(None, None)
        if not self._handle:
            raise OSError("journal_process_group_unavailable")
        limits = _Extended()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, no breakaway.
        if (not kernel.SetInformationJobObject(self._handle, 9, ctypes.byref(limits), ctypes.sizeof(limits))
                or not kernel.AssignProcessToJobObject(self._handle, process_handle)):
            self.close()
            raise OSError("journal_process_group_unavailable")

    def close(self) -> None:
        if self._handle:
            self._kernel.CloseHandle(self._handle)
            self._handle = None
