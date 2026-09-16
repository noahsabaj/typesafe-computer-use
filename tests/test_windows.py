import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows adapter")


@pytest.fixture(scope="module")
def win():
    from typesafe_computer_use import windows

    return windows


def test_normalize_url_adds_scheme_and_rejects_non_urls(win):
    assert win.normalize_url("example.com/path") == "https://example.com/path"
    assert win.normalize_url("https://a.b/c?x=1") == "https://a.b/c?x=1"
    assert win.normalize_url("search terms") is None
    assert win.normalize_url("localhost") is None
    assert win.normalize_url("") is None


def test_field_role_maps_editable_controls_only(win):
    assert win.field_role("EditControl", True) == "AXTextField"
    assert win.field_role("ComboBoxControl", False) == "AXComboBox"
    assert win.field_role("DocumentControl", True) == "AXTextArea"
    assert win.field_role("DocumentControl", False) == "DocumentControl"
    assert win.field_role("ButtonControl", False) == "ButtonControl"


def test_wheel_notches_keeps_sign_and_never_zero(win):
    assert win.wheel_notches(-10) == -3
    assert win.wheel_notches(10) == 3
    assert win.wheel_notches(-1) == -1
    assert win.wheel_notches(1) == 1


def test_app_name_strips_exe_and_case(win):
    assert win.app_name("Chrome.EXE") == "chrome"
    assert win.app_name("msedge") == "msedge"


def test_host_exposes_the_adapter_surface():
    from typesafe_computer_use import host

    for name in (
        "screenshot",
        "display_scale",
        "ocr_lines",
        "focused_field",
        "frontmost_app",
        "browser_url",
        "activate",
        "open_url",
        "click_at",
        "press",
        "type_text",
        "clear_field",
        "scroll",
        "frontmost_window_center",
        "check_abort",
        "sleep_watching",
        "accessibility_trusted",
        "open_file",
        "DEFAULT_BROWSER",
        "FONT_PATH",
    ):
        assert hasattr(host, name), name
