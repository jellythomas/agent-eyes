"""Every platform adapter must implement the full PlatformAdapter Protocol."""
from __future__ import annotations

import inspect
import pytest
from agent_eyes.adapters.base import PlatformAdapter


def _get_protocol_methods() -> list[str]:
    """Return the public method names declared on PlatformAdapter."""
    return [
        name
        for name, member in inspect.getmembers(PlatformAdapter)
        if not name.startswith("_") and callable(member)
    ]


# ---------------------------------------------------------------------------
# Method presence
# ---------------------------------------------------------------------------

def test_macos_implements_protocol():
    from agent_eyes.adapters.macos import MacOSAdapter
    for method in _get_protocol_methods():
        assert hasattr(MacOSAdapter, method), f"MacOSAdapter missing: {method}"


def test_linux_implements_protocol():
    from agent_eyes.adapters.linux import LinuxAdapter
    for method in _get_protocol_methods():
        assert hasattr(LinuxAdapter, method), f"LinuxAdapter missing: {method}"


def test_windows_implements_protocol():
    from agent_eyes.adapters.windows import WindowsAdapter
    for method in _get_protocol_methods():
        assert hasattr(WindowsAdapter, method), f"WindowsAdapter missing: {method}"


# ---------------------------------------------------------------------------
# No NotImplementedError stubs
# ---------------------------------------------------------------------------

def test_macos_no_not_implemented():
    """No method body should raise NotImplementedError."""
    from agent_eyes.adapters.macos import MacOSAdapter
    source = inspect.getsource(MacOSAdapter)
    assert "NotImplementedError" not in source


def test_linux_no_not_implemented():
    from agent_eyes.adapters.linux import LinuxAdapter
    source = inspect.getsource(LinuxAdapter)
    assert "NotImplementedError" not in source


def test_windows_no_not_implemented():
    from agent_eyes.adapters.windows import WindowsAdapter
    source = inspect.getsource(WindowsAdapter)
    assert "NotImplementedError" not in source


# ---------------------------------------------------------------------------
# Protocol export sanity
# ---------------------------------------------------------------------------

def test_platform_adapter_is_runtime_checkable():
    """PlatformAdapter must be decorated with @runtime_checkable."""
    # isinstance() raises TypeError for non-runtime-checkable Protocols
    class Dummy:
        def is_available(self): ...
        def check_permissions(self): ...
        def list_apps(self): ...
        def get_tree(self, pid, max_depth=5): ...
        def get_browser_trees(self, pid, max_depth=6): ...
        def find_elements(self, pid, role="", name="", value=""): ...
        def perform_action(self, element, action): ...
        def focus_element(self, element): ...
        def is_same_element(self, first, second): ...
        def is_element_valid(self, element): ...
        def element_at_position(self, x, y): ...
        def focus_window(self, window): ...
        def is_window_focused(self, window): ...
        def set_value(self, element, value): ...
        def get_focused_element(self): ...
        def get_subtree(self, pid, element_id, max_depth=5): ...
        def is_element_selected(self, element): ...

    # Should not raise TypeError
    result = isinstance(Dummy(), PlatformAdapter)
    assert result is True


def test_protocol_method_count():
    """PlatformAdapter must expose the complete native adapter contract."""
    methods = _get_protocol_methods()
    assert len(methods) == 17, (
        f"Expected 17 protocol methods, got {len(methods)}: {methods}"
    )
