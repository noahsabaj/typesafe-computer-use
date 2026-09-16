"""Windows adapter: synthetic input, app control, screen capture, OCR, and the focused UI Automation element.

This is the only module that touches Win32, UI Automation, or WinRT. It exposes the same
names as macos.py. "Points" are whatever the mouse and window APIs use in this process.
The process is made per-monitor DPI aware at import, so points equal physical pixels and
the capture scale is 1.0 on every display. UI Automation always reports physical pixels,
so its rectangles are divided by the scale to stay in points even if that call failed.
"""

from __future__ import annotations

import asyncio
import ctypes
import os
import shutil
import subprocess
import time
import winreg
from ctypes import wintypes
from pathlib import Path

from PIL import Image, ImageGrab
from pynput.keyboard import Controller as Keyboard
from pynput.keyboard import Key
from pynput.mouse import Button
from pynput.mouse import Controller as Mouse

from .config import ABORT_CORNER_PX
from .models import Abort, Field, Line

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
gdi32 = ctypes.windll.gdi32

DEFAULT_BROWSER = "chrome"  # the executable name, as `frontmost_app` reports it
FONT_PATH = r"C:\Windows\Fonts\arial.ttf"
KEYS = {"return": Key.enter, "tab": Key.tab, "escape": Key.esc, "a": "a", "delete": Key.backspace}
ROLE_BY_CONTROL = {"EditControl": "AXTextField", "ComboBoxControl": "AXComboBox", "DocumentControl": "AXTextArea"}
ADDRESS_BAR_NAMES = ("Address and search bar", "Search or enter web address")  # Chrome and Edge, then Firefox
WHEEL_LINES = 3  # one wheel notch scrolls this many lines by default
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SW_RESTORE = 9
DESKTOPHORZRES = 118


def _dpi_aware() -> None:
    """Make mouse, window, and capture coordinates all physical pixels. Must run before any window call."""
    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
    except (AttributeError, OSError):
        user32.SetProcessDPIAware()


_dpi_aware()
_keyboard = Keyboard()
_mouse = Mouse()

# ------------------------------------------------------------------ escape hatch


def mouse_location() -> tuple[float, float]:
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return float(pt.x), float(pt.y)


def check_abort() -> None:
    x, y = mouse_location()
    if x <= ABORT_CORNER_PX and y <= ABORT_CORNER_PX:
        raise Abort("mouse in top-left corner")


def sleep_watching(seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        check_abort()
        time.sleep(0.1)


def accessibility_trusted() -> bool:
    return True  # Windows needs no permission grant for synthetic input


# ------------------------------------------------------------------ input


def click_at(point: tuple[float, float]) -> None:
    _mouse.position = (round(point[0]), round(point[1]))
    time.sleep(0.04)
    _mouse.click(Button.left)
    time.sleep(0.04)


def press(key: str, command: bool = False) -> None:
    k = KEYS[key]
    if command:  # Command on a Mac is Ctrl here
        with _keyboard.pressed(Key.ctrl):
            _keyboard.tap(k)
    else:
        _keyboard.tap(k)
    time.sleep(0.04)


def type_text(text: str) -> None:
    _keyboard.type(text)
    time.sleep(0.04)


def clear_field() -> None:
    press("a", command=True)
    press("delete")


def scroll(lines: int) -> None:
    """Wheel events go to the window under the cursor, so park it over the frontmost window first."""
    center = frontmost_window_center()
    if center is not None:
        _mouse.position = (int(center[0]), int(center[1]))
        time.sleep(0.04)
    _mouse.scroll(0, wheel_notches(lines))
    time.sleep(0.04)


def wheel_notches(lines: int) -> int:
    """Lines to wheel notches, keeping the sign (positive is up, as on macOS) and never rounding to zero."""
    notches = round(lines / WHEEL_LINES)
    return notches or (1 if lines > 0 else -1)


# ------------------------------------------------------------------ apps and windows


def _process_name(pid: int) -> str:
    """Executable basename without .exe, lower case: 'chrome', 'msedge', 'windowsterminal'."""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return ""
        return Path(buf.value).stem.lower()
    finally:
        kernel32.CloseHandle(handle)


def _window_pid(hwnd: int) -> int:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def app_name(app: str) -> str:
    return app.lower().removesuffix(".exe")


def frontmost_hwnd() -> int:
    return user32.GetForegroundWindow()


def frontmost_app() -> str:
    hwnd = frontmost_hwnd()
    return _process_name(_window_pid(hwnd)) if hwnd else ""


def frontmost_pid() -> int:
    return _window_pid(frontmost_hwnd())


def _windows_of(app: str) -> list[int]:
    """Visible, titled top-level windows owned by a process named `app`, in z-order."""
    want = app_name(app)
    found: list[int] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _):
        if user32.IsWindowVisible(hwnd) and user32.GetWindowTextLengthW(hwnd) > 0 and _process_name(_window_pid(hwnd)) == want:
            found.append(hwnd)
        return True

    user32.EnumWindows(visit, 0)
    return found


def activate(app: str, timeout: float = 3.0) -> bool:
    """Bring an app's front window forward and confirm it got there."""
    want = app_name(app)
    windows = _windows_of(want)
    if not windows:
        return False
    hwnd = windows[0]
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    _keyboard.tap(Key.alt)  # a keypress from this process lets SetForegroundWindow succeed
    user32.SetForegroundWindow(hwnd)
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if frontmost_app() == want:
            return True
        time.sleep(0.1)
    user32.SwitchToThisWindow(hwnd, True)
    time.sleep(0.3)
    return frontmost_app() == want


def browser_executable(browser: str) -> str | None:
    """Resolve 'chrome' or 'msedge' to a path via PATH or the App Paths registry."""
    exe = app_name(browser) + ".exe"
    if found := shutil.which(exe):
        return found
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(root, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}") as key:
                return winreg.QueryValue(key, None) or None
        except OSError:
            continue
    return None


def open_url(browser: str, url: str) -> bool:
    exe = browser_executable(browser)
    if exe:
        subprocess.Popen([exe, url], creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        os.startfile(url)  # the default browser, whatever it is
    time.sleep(0.5)
    return activate(browser)


def normalize_url(shown: str) -> str | None:
    """What a browser's address bar displays, made into a URL. Chrome hides the scheme."""
    shown = shown.strip()
    url = shown if "://" in shown else "https://" + shown
    host = url.split("://", 1)[1].split("/")[0]
    if not shown or " " in shown or "." not in host:
        return None
    return url


def browser_url(browser: str) -> str | None:
    hwnd = frontmost_hwnd()
    if not hwnd or frontmost_app() != app_name(browser):
        return None
    try:
        import uiautomation as auto

        window = auto.ControlFromHandle(hwnd)
        for name in ADDRESS_BAR_NAMES:
            edit = window.EditControl(Name=name, searchDepth=12)
            if edit.Exists(maxSearchSeconds=1, searchIntervalSeconds=0.2):
                return normalize_url(edit.GetValuePattern().Value or "")
    except Exception:
        return None
    return None


def frontmost_window_center() -> tuple[float, float] | None:
    hwnd = frontmost_hwnd()
    if not hwnd:
        return None
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    w, h = rect.right - rect.left, rect.bottom - rect.top
    if w <= 50 or h <= 50:
        return None
    return rect.left + w / 2, rect.top + h / 2


# ------------------------------------------------------------------ capture and accessibility


def screenshot() -> Image.Image:
    return ImageGrab.grab().convert("RGB")


def display_scale(image: Image.Image) -> float:
    return image.width / user32.GetSystemMetrics(0)  # SM_CXSCREEN, in this process's coordinate space


def physical_scale() -> float:
    """Physical pixels per point on the main display, without taking a capture."""
    device = user32.GetDC(0)
    try:
        physical = gdi32.GetDeviceCaps(device, DESKTOPHORZRES)
    finally:
        user32.ReleaseDC(0, device)
    return physical / user32.GetSystemMetrics(0)


def field_role(control_type: str, editable: bool) -> str:
    """Map a UI Automation control type onto the AX role vocabulary the rest of the code keys on."""
    if control_type == "DocumentControl" and not editable:
        return control_type  # a web page body, not a field
    return ROLE_BY_CONTROL.get(control_type, control_type)


def focused_field() -> Field | None:
    try:
        import uiautomation as auto

        control = auto.GetFocusedControl()
        if control is None:
            return None
        value, editable = "", False
        if control.GetPattern(auto.PatternId.ValuePattern):
            pattern = control.GetValuePattern()
            value, editable = pattern.Value or "", not pattern.IsReadOnly
        scale = physical_scale()
        r = control.BoundingRectangle
        return Field(
            role=field_role(control.ControlTypeName, editable),
            label=control.Name or "",
            placeholder=control.HelpText or "",
            value=value if isinstance(value, str) else "",
            x=r.left / scale,
            y=r.top / scale,
            w=(r.right - r.left) / scale,
            h=(r.bottom - r.top) / scale,
        )
    except Exception:
        return None


def ocr_lines(image: Image.Image) -> list[Line]:
    """Windows OCR (Windows.Media.Ocr): one entry per recognised line, box in capture pixels. No confidences."""
    return asyncio.run(_ocr(image))


async def _ocr(image: Image.Image) -> list[Line]:
    from winrt.windows.graphics.imaging import BitmapAlphaMode, BitmapPixelFormat, SoftwareBitmap
    from winrt.windows.media.ocr import OcrEngine

    engine = OcrEngine.try_create_from_user_profile_languages()
    if engine is None:
        raise RuntimeError("Windows OCR has no language installed; add one under Settings > Time & language > Language")
    factor = max(1.0, max(image.size) / OcrEngine.max_image_dimension)
    src = image.resize((int(image.width / factor), int(image.height / factor))) if factor > 1 else image
    bgra = src.convert("RGBA").tobytes("raw", "BGRA")
    bitmap = SoftwareBitmap.create_copy_with_alpha_from_buffer(
        bgra, BitmapPixelFormat.BGRA8, src.width, src.height, BitmapAlphaMode.IGNORE
    )
    result = await engine.recognize_async(bitmap)
    lines: list[Line] = []
    for line in result.lines:
        rects = [w.bounding_rect for w in line.words]
        if not rects:
            continue
        box = (
            min(r.x for r in rects) * factor,
            min(r.y for r in rects) * factor,
            max(r.x + r.width for r in rects) * factor,
            max(r.y + r.height for r in rects) * factor,
        )
        lines.append((line.text, 1.0, box))
    return lines


def open_file(path: Path, as_text: bool = False) -> None:
    if as_text:
        subprocess.Popen(["notepad", str(path)])
    else:
        os.startfile(path)
