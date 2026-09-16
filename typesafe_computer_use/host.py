"""The platform adapter for this machine.

Exactly one of macos.py or windows.py is loaded. Every other module imports `host` and
calls the same names: screenshot, display_scale, ocr_lines, focused_field, frontmost_app,
browser_url, activate, open_url, click_at, press, type_text, clear_field, scroll,
frontmost_window_center, check_abort, sleep_watching, accessibility_trusted, open_file,
plus the constants DEFAULT_BROWSER and FONT_PATH.
"""

from __future__ import annotations

import sys

if sys.platform == "darwin":
    from .macos import *  # noqa: F403
elif sys.platform == "win32":
    from .windows import *  # noqa: F403
else:
    raise ImportError(f"no platform adapter for {sys.platform}; see macos.py and windows.py")
