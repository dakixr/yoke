"""Windows DACL protection for the daemon's local bearer capability."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from pathlib import Path
from typing import Any, BinaryIO


def _user_sid(advapi: Any, kernel: Any) -> str:
    """Read the current process user's SID, including domain accounts."""
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    advapi.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi.OpenProcessToken.restype = wintypes.BOOL
    advapi.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi.GetTokenInformation.restype = wintypes.BOOL
    advapi.ConvertSidToStringSidW.argtypes = [
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.LPWSTR),
    ]
    advapi.ConvertSidToStringSidW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    token = wintypes.HANDLE()
    if not advapi.OpenProcessToken(
        kernel.GetCurrentProcess(), 0x8, ctypes.byref(token)
    ):
        raise getattr(ctypes, "WinError")()
    try:
        size = wintypes.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)):
            raise getattr(ctypes, "WinError")()
        # TOKEN_USER begins with a SID_AND_ATTRIBUTES, whose first field is PSID.
        sid = ctypes.cast(buffer, ctypes.POINTER(wintypes.LPVOID))[0]
        text = wintypes.LPWSTR()
        if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            raise getattr(ctypes, "WinError")()
        try:
            return str(text.value)
        finally:
            kernel.LocalFree(ctypes.cast(text, wintypes.LPVOID))
    finally:
        kernel.CloseHandle(token)


def protect_path(path: Path, *, directory: bool = False) -> None:
    """Grant only the current user access, replacing all inherited ACEs.

    Children inherit the user's full-control ACE. Fail closed if the filesystem
    cannot apply a protected DACL rather than publishing an exposed token.
    """
    advapi = getattr(ctypes, "WinDLL")("advapi32", use_last_error=True)
    kernel = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [wintypes.LPVOID]
    kernel.LocalFree.restype = wintypes.LPVOID
    convert = advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.DWORD),
    ]
    convert.restype = wintypes.BOOL
    set_security = advapi.SetFileSecurityW
    set_security.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPVOID]
    set_security.restype = wintypes.BOOL
    descriptor = wintypes.LPVOID()
    inheritance = "OICI" if directory else ""
    sid = _user_sid(advapi, kernel)
    if not convert(
        f"D:P(A;{inheritance};FA;;;{sid})", 1, ctypes.byref(descriptor), None
    ):
        raise getattr(ctypes, "WinError")()
    try:
        # DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION
        if not set_security(str(path), 0x80000004, descriptor):
            raise getattr(ctypes, "WinError")()
    finally:
        kernel.LocalFree(descriptor)


def open_log(path: Path) -> BinaryIO:
    """Keep inherited log handles compatible with rename during rotation."""
    import os
    import msvcrt

    kernel = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
    create_file = kernel.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    # GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
    # OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL. Python's normal open omits DELETE.
    handle = create_file(str(path), 0x40000000, 0x7, None, 4, 0x80, None)
    if handle == wintypes.HANDLE(-1).value:
        raise getattr(ctypes, "WinError")()
    try:
        descriptor = getattr(msvcrt, "open_osfhandle")(
            handle, os.O_WRONLY | os.O_APPEND | getattr(os, "O_BINARY")
        )
    except BaseException:
        kernel.CloseHandle(handle)
        raise
    return os.fdopen(descriptor, "ab")
