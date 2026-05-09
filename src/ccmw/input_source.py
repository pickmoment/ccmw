"""macOS 키보드 입력 소스(한/영) 감지.

TIS API는 GUI 없는 터미널 프로세스에서 올바른 결과를 반환하지 않으므로
CFPreferences로 com.apple.HIToolbox 설정을 직접 읽는다.
"""
from __future__ import annotations

import ctypes
import sys

_initialized = False
_cf: ctypes.CDLL | None = None
_kCurrentUser: int | None = None
_kAnyHost: int | None = None
_kUTF8 = 0x08000100


def _setup() -> bool:
    global _initialized, _cf, _kCurrentUser, _kAnyHost
    if _initialized:
        return _cf is not None
    _initialized = True
    if sys.platform != "darwin":
        return False
    try:
        cf = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
        )
        cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
        cf.CFStringGetLength.restype = ctypes.c_long
        cf.CFStringGetLength.argtypes = [ctypes.c_void_p]
        cf.CFStringGetCString.restype = ctypes.c_bool
        cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
        cf.CFRelease.restype = None
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        cf.CFCopyDescription.restype = ctypes.c_void_p
        cf.CFCopyDescription.argtypes = [ctypes.c_void_p]
        cf.CFPreferencesCopyValue.restype = ctypes.c_void_p
        cf.CFPreferencesCopyValue.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p
        ]
        cf.CFPreferencesSynchronize.restype = ctypes.c_bool
        cf.CFPreferencesSynchronize.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p
        ]
        _kCurrentUser = ctypes.c_void_p.in_dll(cf, "kCFPreferencesCurrentUser").value
        _kAnyHost = ctypes.c_void_p.in_dll(cf, "kCFPreferencesAnyHost").value
        _cf = cf
        return True
    except Exception:
        return False


def _cfstr(s: bytes) -> int:
    return _cf.CFStringCreateWithCString(None, s, _kUTF8)


def _cfstr_to_py(ref: int) -> str:
    length = _cf.CFStringGetLength(ref)
    buf = ctypes.create_string_buffer((length + 1) * 4)
    _cf.CFStringGetCString(ref, buf, len(buf), _kUTF8)
    return buf.value.decode("utf-8", errors="replace")


def get_input_language() -> str:
    """현재 입력 소스가 한국어면 '한', 아니면 'EN'. 비macOS는 '' 반환."""
    if sys.platform != "darwin":
        return ""
    if not _setup():
        return "EN"
    domain_ref = key_ref = value_ref = desc_ref = None
    try:
        domain_ref = _cfstr(b"com.apple.HIToolbox")
        key_ref = _cfstr(b"AppleSelectedInputSources")

        _cf.CFPreferencesSynchronize(domain_ref, _kCurrentUser, _kAnyHost)
        value_ref = _cf.CFPreferencesCopyValue(key_ref, domain_ref, _kCurrentUser, _kAnyHost)

        if not value_ref:
            return "EN"

        desc_ref = _cf.CFCopyDescription(value_ref)
        if not desc_ref:
            return "EN"

        desc = _cfstr_to_py(desc_ref)
        return "한" if "Korean" in desc else "EN"
    except Exception:
        return "EN"
    finally:
        for ref in (domain_ref, key_ref, value_ref, desc_ref):
            if ref:
                _cf.CFRelease(ref)
