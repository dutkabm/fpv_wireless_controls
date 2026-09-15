"""macOS IOHID joystick backend.

Pygame/SDL 2.28 often reports ``get_count() == 0`` for generic USB HID radios
(EdgeTX / OpenTX, e.g. Radiomaster TX12) that show up as ``AppleUserHIDDevice``.
IOKit still enumerates those devices; this module duck-types
``pygame.joystick.Joystick`` so the ground station can read them.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import (
    byref,
    c_bool,
    c_char_p,
    c_double,
    c_int,
    c_int32,
    c_long,
    c_uint32,
    c_void_p,
)
from dataclasses import dataclass
from typing import List, Optional, Tuple

_LOG = logging.getLogger(__name__)

_KCF_NUMBER_SINT32 = 3
_KCF_STRING_UTF8 = 0x08000100
_ELEM_INPUT_MISC = 1
_ELEM_INPUT_BUTTON = 2
_ELEM_INPUT_AXIS = 3
_PAGE_GENERIC_DESKTOP = 1
_PAGE_SIMULATION = 2
_PAGE_BUTTON = 9
_PAGE_CONSUMER = 12
_USAGE_JOYSTICK = 4
_USAGE_GAMEPAD = 5
_USAGE_MULTI_AXIS = 8
_AXIS_USAGES = frozenset({0x30, 0x31, 0x32, 0x33, 0x34, 0x35, 0x36, 0x37, 0x38})
_SIM_AXIS_USAGES = frozenset({0xBA, 0xBB, 0xC4, 0xC5})
_HAT_USAGE = 0x39

_HAT_TO_XY = {
    0: (0, 1),
    1: (1, 1),
    2: (1, 0),
    3: (1, -1),
    4: (0, -1),
    5: (-1, -1),
    6: (-1, 0),
    7: (-1, 1),
}


def _as_ptr(val: object) -> int:
    if val is None:
        return 0
    if isinstance(val, int):
        return val
    value = getattr(val, "value", val)
    if value is None:
        return 0
    return int(value)


@dataclass(frozen=True)
class _HidElement:
    ref: int
    cookie: int
    usage: int
    logical_min: int
    logical_max: int


class _IOHID:
    def __init__(self) -> None:
        self.cf = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
        )
        self.iokit = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/IOKit.framework/IOKit")
        cf = self.cf
        iokit = self.iokit

        self.kCFTypeDictionaryKeyCallBacks = c_void_p.in_dll(cf, "kCFTypeDictionaryKeyCallBacks")
        self.kCFTypeDictionaryValueCallBacks = c_void_p.in_dll(cf, "kCFTypeDictionaryValueCallBacks")
        self.kCFTypeArrayCallBacks = c_void_p.in_dll(cf, "kCFTypeArrayCallBacks")
        self.kCFRunLoopDefaultMode = c_void_p.in_dll(cf, "kCFRunLoopDefaultMode")

        cf.CFNumberCreate.restype = c_void_p
        cf.CFNumberCreate.argtypes = [c_void_p, c_int, c_void_p]
        cf.CFStringCreateWithCString.restype = c_void_p
        cf.CFStringCreateWithCString.argtypes = [c_void_p, c_char_p, c_uint32]
        cf.CFDictionaryCreate.restype = c_void_p
        cf.CFDictionaryCreate.argtypes = [c_void_p, c_void_p, c_void_p, c_long, c_void_p, c_void_p]
        cf.CFArrayCreate.restype = c_void_p
        cf.CFArrayCreate.argtypes = [c_void_p, c_void_p, c_long, c_void_p]
        cf.CFArrayGetCount.restype = c_long
        cf.CFArrayGetCount.argtypes = [c_void_p]
        cf.CFArrayGetValueAtIndex.restype = c_void_p
        cf.CFArrayGetValueAtIndex.argtypes = [c_void_p, c_long]
        cf.CFSetGetCount.restype = c_long
        cf.CFSetGetCount.argtypes = [c_void_p]
        cf.CFSetGetValues.argtypes = [c_void_p, c_void_p]
        cf.CFRunLoopRunInMode.restype = c_int32
        cf.CFRunLoopRunInMode.argtypes = [c_void_p, c_double, c_bool]
        cf.CFRunLoopGetCurrent.restype = c_void_p
        cf.CFStringGetCString.restype = c_bool
        cf.CFStringGetCString.argtypes = [c_void_p, c_char_p, c_long, c_uint32]
        cf.CFNumberGetValue.restype = c_bool
        cf.CFNumberGetValue.argtypes = [c_void_p, c_int, c_void_p]
        cf.CFRetain.restype = c_void_p
        cf.CFRetain.argtypes = [c_void_p]
        cf.CFRelease.argtypes = [c_void_p]

        iokit.IOHIDManagerCreate.restype = c_void_p
        iokit.IOHIDManagerCreate.argtypes = [c_void_p, c_int]
        iokit.IOHIDManagerOpen.restype = c_int
        iokit.IOHIDManagerOpen.argtypes = [c_void_p, c_int]
        iokit.IOHIDManagerSetDeviceMatchingMultiple.argtypes = [c_void_p, c_void_p]
        iokit.IOHIDManagerCopyDevices.restype = c_void_p
        iokit.IOHIDManagerCopyDevices.argtypes = [c_void_p]
        iokit.IOHIDManagerScheduleWithRunLoop.argtypes = [c_void_p, c_void_p, c_void_p]
        iokit.IOHIDDeviceGetProperty.restype = c_void_p
        iokit.IOHIDDeviceGetProperty.argtypes = [c_void_p, c_void_p]
        iokit.IOHIDDeviceOpen.restype = c_int
        iokit.IOHIDDeviceOpen.argtypes = [c_void_p, c_int]
        iokit.IOHIDDeviceClose.restype = c_int
        iokit.IOHIDDeviceClose.argtypes = [c_void_p, c_int]
        iokit.IOHIDDeviceCopyMatchingElements.restype = c_void_p
        iokit.IOHIDDeviceCopyMatchingElements.argtypes = [c_void_p, c_void_p, c_int]
        iokit.IOHIDElementGetType.restype = c_int
        iokit.IOHIDElementGetType.argtypes = [c_void_p]
        iokit.IOHIDElementGetUsagePage.restype = c_uint32
        iokit.IOHIDElementGetUsagePage.argtypes = [c_void_p]
        iokit.IOHIDElementGetUsage.restype = c_uint32
        iokit.IOHIDElementGetUsage.argtypes = [c_void_p]
        iokit.IOHIDElementGetLogicalMin.restype = c_long
        iokit.IOHIDElementGetLogicalMin.argtypes = [c_void_p]
        iokit.IOHIDElementGetLogicalMax.restype = c_long
        iokit.IOHIDElementGetLogicalMax.argtypes = [c_void_p]
        iokit.IOHIDElementGetCookie.restype = c_uint32
        iokit.IOHIDElementGetCookie.argtypes = [c_void_p]
        iokit.IOHIDDeviceGetValue.restype = c_int
        iokit.IOHIDDeviceGetValue.argtypes = [c_void_p, c_void_p, c_void_p]
        iokit.IOHIDValueGetIntegerValue.restype = c_long
        iokit.IOHIDValueGetIntegerValue.argtypes = [c_void_p]

        self._mgr = 0

    def cfstr(self, s: str) -> int:
        return _as_ptr(self.cf.CFStringCreateWithCString(None, s.encode(), _KCF_STRING_UTF8))

    def cfnum(self, n: int) -> int:
        v = c_int(n)
        return _as_ptr(self.cf.CFNumberCreate(None, _KCF_NUMBER_SINT32, byref(v)))

    def cf_string(self, ref: int) -> str:
        if not ref:
            return ""
        buf = ctypes.create_string_buffer(256)
        if self.cf.CFStringGetCString(ref, buf, 256, _KCF_STRING_UTF8):
            return buf.value.decode("utf-8", errors="replace")
        return ""

    def cf_int(self, ref: int) -> int:
        if not ref:
            return 0
        v = c_int()
        if self.cf.CFNumberGetValue(ref, _KCF_NUMBER_SINT32, byref(v)):
            return int(v.value)
        return 0

    def device_str(self, dev: int, key: str) -> str:
        return self.cf_string(_as_ptr(self.iokit.IOHIDDeviceGetProperty(dev, self.cfstr(key))))

    def device_int(self, dev: int, key: str) -> int:
        return self.cf_int(_as_ptr(self.iokit.IOHIDDeviceGetProperty(dev, self.cfstr(key))))

    def match_array(self) -> int:
        def one(page: int, usage: int) -> int:
            keys = (c_void_p * 2)(self.cfstr("DeviceUsagePage"), self.cfstr("DeviceUsage"))
            vals = (c_void_p * 2)(self.cfnum(page), self.cfnum(usage))
            return _as_ptr(
                self.cf.CFDictionaryCreate(
                    None,
                    keys,
                    vals,
                    2,
                    byref(self.kCFTypeDictionaryKeyCallBacks),
                    byref(self.kCFTypeDictionaryValueCallBacks),
                )
            )

        dicts = (c_void_p * 3)(
            one(_PAGE_GENERIC_DESKTOP, _USAGE_JOYSTICK),
            one(_PAGE_GENERIC_DESKTOP, _USAGE_GAMEPAD),
            one(_PAGE_GENERIC_DESKTOP, _USAGE_MULTI_AXIS),
        )
        return _as_ptr(self.cf.CFArrayCreate(None, dicts, 3, byref(self.kCFTypeArrayCallBacks)))

    def manager(self) -> int:
        if self._mgr:
            self.pump(2)
            return self._mgr
        mgr = _as_ptr(self.iokit.IOHIDManagerCreate(None, 0))
        if not mgr:
            raise OSError("IOHIDManagerCreate failed")
        rc = self.iokit.IOHIDManagerOpen(mgr, 0) & 0xFFFFFFFF
        if rc != 0:
            raise OSError(f"IOHIDManagerOpen failed: {rc:#x}")
        matching = self.match_array()
        self.iokit.IOHIDManagerSetDeviceMatchingMultiple(mgr, matching)
        self.iokit.IOHIDManagerScheduleWithRunLoop(
            mgr, self.cf.CFRunLoopGetCurrent(), self.kCFRunLoopDefaultMode
        )
        if matching:
            self.cf.CFRelease(matching)
        self._mgr = mgr
        self.pump(12)
        return mgr

    def pump(self, iterations: int = 8) -> None:
        for _ in range(iterations):
            self.cf.CFRunLoopRunInMode(self.kCFRunLoopDefaultMode, 0.01, True)


_api: Optional[_IOHID] = None


def _hid() -> _IOHID:
    global _api
    if _api is None:
        _api = _IOHID()
    return _api


def _copy_devices() -> List[int]:
    api = _hid()
    mgr = api.manager()
    raw = _as_ptr(api.iokit.IOHIDManagerCopyDevices(mgr))
    if not raw:
        return []
    n = int(api.cf.CFSetGetCount(raw))
    if n <= 0:
        api.cf.CFRelease(raw)
        return []
    buf = (c_void_p * n)()
    api.cf.CFSetGetValues(raw, buf)
    out = [_as_ptr(buf[i]) for i in range(n)]
    api.cf.CFRelease(raw)
    return [d for d in out if d]


@dataclass(frozen=True)
class _DeviceInfo:
    name: str
    vendor_id: int
    product_id: int
    location_id: int
    device: int


def _info_from_device(dev: int) -> _DeviceInfo:
    api = _hid()
    name = api.device_str(dev, "Product") or "HID joystick"
    return _DeviceInfo(
        name=name,
        vendor_id=api.device_int(dev, "VendorID"),
        product_id=api.device_int(dev, "ProductID"),
        location_id=api.device_int(dev, "LocationID"),
        device=dev,
    )


def list_macos_hid_joystick_names() -> List[str]:
    infos = [_info_from_device(d) for d in _copy_devices()]
    infos.sort(key=lambda i: (i.location_id, i.vendor_id, i.product_id, i.name))
    return [i.name for i in infos]


class MacOSHidJoystick:
    """Duck-typed pygame joystick backed by IOHIDDeviceGetValue."""

    def __init__(self, device: int, name: str) -> None:
        self._dev = device
        self._name = name
        self._opened = False
        self._axes: List[_HidElement] = []
        self._buttons: List[_HidElement] = []
        self._hats: List[_HidElement] = []
        self.init()

    def init(self) -> None:
        if self._opened or not self._dev:
            return
        api = _hid()
        rc = api.iokit.IOHIDDeviceOpen(self._dev, 0) & 0xFFFFFFFF
        if rc != 0:
            raise OSError(f"IOHIDDeviceOpen failed: {rc:#x}")
        api.cf.CFRetain(self._dev)
        self._opened = True
        self._collect_elements()

    def quit(self) -> None:
        api = _hid()
        if self._opened and self._dev:
            try:
                api.iokit.IOHIDDeviceClose(self._dev, 0)
            except Exception:
                pass
            try:
                api.cf.CFRelease(self._dev)
            except Exception:
                pass
            self._opened = False
        self._dev = 0
        self._axes = []
        self._buttons = []
        self._hats = []

    def get_name(self) -> str:
        return self._name

    def get_numaxes(self) -> int:
        return len(self._axes)

    def get_numbuttons(self) -> int:
        return len(self._buttons)

    def get_numhats(self) -> int:
        return len(self._hats)

    def get_axis(self, index: int) -> float:
        el = self._axes[index]
        raw = self._read_int(el)
        span = el.logical_max - el.logical_min
        if span <= 0:
            return 0.0
        v = 2.0 * (raw - el.logical_min) / span - 1.0
        return max(-1.0, min(1.0, v))

    def get_button(self, index: int) -> int:
        el = self._buttons[index]
        return 1 if self._read_int(el) else 0

    def get_hat(self, index: int) -> Tuple[int, int]:
        el = self._hats[index]
        raw = self._read_int(el)
        if el.logical_max - el.logical_min > 7 and raw >= el.logical_max:
            return (0, 0)
        return _HAT_TO_XY.get(int(raw), (0, 0))

    def _read_int(self, el: _HidElement) -> int:
        if not self._dev or not self._opened:
            return 0
        api = _hid()
        vref = c_void_p()
        rc = api.iokit.IOHIDDeviceGetValue(self._dev, el.ref, byref(vref))
        if rc != 0 or not vref.value:
            return 0
        return int(api.iokit.IOHIDValueGetIntegerValue(vref))

    def _collect_elements(self) -> None:
        api = _hid()
        arr = _as_ptr(api.iokit.IOHIDDeviceCopyMatchingElements(self._dev, None, 0))
        if not arr:
            return
        n = int(api.cf.CFArrayGetCount(arr))
        axes: List[_HidElement] = []
        buttons: List[_HidElement] = []
        hats: List[_HidElement] = []
        seen: set[int] = set()
        for i in range(n):
            el = _as_ptr(api.cf.CFArrayGetValueAtIndex(arr, i))
            if not el:
                continue
            et = api.iokit.IOHIDElementGetType(el)
            if et not in (_ELEM_INPUT_MISC, _ELEM_INPUT_BUTTON, _ELEM_INPUT_AXIS):
                continue
            cookie = int(api.iokit.IOHIDElementGetCookie(el))
            if cookie in seen:
                continue
            page = int(api.iokit.IOHIDElementGetUsagePage(el))
            usage = int(api.iokit.IOHIDElementGetUsage(el))
            kind = _classify(page, usage)
            if kind is None:
                continue
            seen.add(cookie)
            rec = _HidElement(
                ref=el,
                cookie=cookie,
                usage=usage,
                logical_min=int(api.iokit.IOHIDElementGetLogicalMin(el)),
                logical_max=int(api.iokit.IOHIDElementGetLogicalMax(el)),
            )
            if kind == "axis":
                axes.append(rec)
            elif kind == "button":
                buttons.append(rec)
            else:
                hats.append(rec)
        api.cf.CFRelease(arr)
        axes.sort(key=lambda e: (e.usage, e.cookie))
        buttons.sort(key=lambda e: (e.usage, e.cookie))
        hats.sort(key=lambda e: (e.usage, e.cookie))
        self._axes = axes
        self._buttons = buttons
        self._hats = hats


def _classify(page: int, usage: int) -> Optional[str]:
    if page == _PAGE_GENERIC_DESKTOP:
        if usage in _AXIS_USAGES:
            return "axis"
        if usage == _HAT_USAGE:
            return "hat"
    elif page == _PAGE_SIMULATION and usage in _SIM_AXIS_USAGES:
        return "axis"
    elif page in (_PAGE_BUTTON, _PAGE_CONSUMER) and usage != 0:
        return "button"
    return None


def open_macos_hid_joystick(index: int) -> Optional[MacOSHidJoystick]:
    infos = [_info_from_device(d) for d in _copy_devices()]
    infos.sort(key=lambda i: (i.location_id, i.vendor_id, i.product_id, i.name))
    if index < 0 or index >= len(infos):
        return None
    info = infos[index]
    joy = MacOSHidJoystick(info.device, info.name)
    _LOG.info(
        "Using macOS HID joystick %d: %s (%d axes, %d buttons, vid=%04x pid=%04x)",
        index,
        info.name,
        joy.get_numaxes(),
        joy.get_numbuttons(),
        info.vendor_id,
        info.product_id,
    )
    return joy
