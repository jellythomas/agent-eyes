from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from types import SimpleNamespace

from agent_eyes.adapters.macos import MacOSAdapter
from agent_eyes.browser_inventory import extract_tab_elements


@dataclass(eq=False)
class FakeAXElement:
    key: int
    role: str
    title: str = ""
    children: list["FakeAXElement"] = field(default_factory=list)
    selected: bool = False
    actions: list[str] = field(default_factory=list)

    def __hash__(self) -> int:
        return self.key


class FakeAX:
    def __init__(self, app: FakeAXElement):
        self.app = app
        self.read_counts: Counter[str] = Counter()
        self.batch_reads = 0
        self.action_reads = 0

    def AXUIElementCreateApplication(self, _pid: int) -> FakeAXElement:
        return self.app

    def AXUIElementCopyAttributeValue(self, element, attr: str, _out):
        self.read_counts[attr] += 1
        values = {
            "AXRole": element.role,
            "AXTitle": element.title,
            "AXChildren": element.children,
            "AXSelected": element.selected,
        }
        value = values.get(attr)
        return (0, value) if value is not None else (-25205, None)

    def AXUIElementCopyMultipleAttributeValues(self, element, attrs, _options, _out):
        self.batch_reads += 1
        values = []
        for attr in attrs:
            result = self.AXUIElementCopyAttributeValue(element, str(attr), None)
            values.append(result[1])
        return 0, values

    def AXUIElementCopyActionNames(self, element, _out):
        self.action_reads += 1
        return 0, element.actions


def _adapter_with_ax(app: FakeAXElement) -> tuple[MacOSAdapter, FakeAX]:
    adapter = MacOSAdapter()
    fake_ax = FakeAX(app)
    adapter._ax = fake_ax
    adapter.force_browser_accessibility = lambda _pid: None
    return adapter, fake_ax


def test_browser_tree_scans_every_window_and_prunes_menu_and_document_content():
    page_tab = FakeAXElement(9, "AXTab", title="Page-owned tab")
    browser_tab_1 = FakeAXElement(
        5,
        "AXRadioButton",
        title="First browser tab",
        selected=True,
        actions=["AXPress"],
    )
    browser_tab_2 = FakeAXElement(
        15,
        "AXRadioButton",
        title="Second browser tab",
        actions=["AXPress"],
    )
    menu_item = FakeAXElement(3, "AXMenuItem", title="Never traverse me")
    menu = FakeAXElement(2, "AXMenuBar", children=[menu_item])
    tab_group_1 = FakeAXElement(4, "AXTabGroup", children=[browser_tab_1])
    tab_group_2 = FakeAXElement(14, "AXTabGroup", children=[browser_tab_2])
    document = FakeAXElement(8, "AXWebArea", children=[page_tab])
    window_1 = FakeAXElement(
        1,
        "AXWindow",
        title="First window",
        children=[menu, tab_group_1, document],
    )
    window_2 = FakeAXElement(
        11,
        "AXWindow",
        title="Second window",
        children=[tab_group_2],
    )
    app = FakeAXElement(100, "AXApplication", children=[window_1, window_2])
    app.windows = [window_1, window_2]
    adapter, fake_ax = _adapter_with_ax(app)

    original_read = fake_ax.AXUIElementCopyAttributeValue

    def read_with_windows(element, attr: str, out):
        if element is app and attr == "AXWindows":
            fake_ax.read_counts[attr] += 1
            return 0, app.windows
        return original_read(element, attr, out)

    fake_ax.AXUIElementCopyAttributeValue = read_with_windows

    trees = adapter.get_browser_trees(42, max_depth=6)

    assert len(trees) == 2
    assert [tree.window_index for tree in trees] == [0, 1]
    assert [[tab.name for tab in extract_tab_elements(tree)] for tree in trees] == [
        ["First browser tab"],
        ["Second browser tab"],
    ]
    assert fake_ax.read_counts["AXRole"] < 20
    assert fake_ax.action_reads == 2


def test_browser_tree_rejects_ax_application_self_reference_without_fallback_walk():
    menu = FakeAXElement(2, "AXMenuBar")
    app = FakeAXElement(1, "AXApplication")
    app.children = [app, menu]
    app.windows = [app]
    adapter, fake_ax = _adapter_with_ax(app)

    original_read = fake_ax.AXUIElementCopyAttributeValue

    def read_with_windows(element, attr: str, out):
        if element is app and attr in {"AXWindows", "AXFocusedWindow", "AXMainWindow"}:
            fake_ax.read_counts[attr] += 1
            return 0, app.windows if attr == "AXWindows" else app
        return original_read(element, attr, out)

    fake_ax.AXUIElementCopyAttributeValue = read_with_windows

    assert adapter.get_browser_trees(42) == []
    assert fake_ax.batch_reads == 0
    assert fake_ax.action_reads == 0


def test_browser_tree_has_an_independent_hard_limit_per_window():
    first_window = FakeAXElement(
        1,
        "AXWindow",
        children=[FakeAXElement(index, "AXGroup") for index in range(10, 710)],
    )
    second_window = FakeAXElement(2, "AXWindow", children=[FakeAXElement(900, "AXTab")])
    app = FakeAXElement(3, "AXApplication")
    app.windows = [first_window, second_window]
    adapter, fake_ax = _adapter_with_ax(app)

    original_read = fake_ax.AXUIElementCopyAttributeValue

    def read_with_windows(element, attr: str, out):
        if element is app and attr == "AXWindows":
            fake_ax.read_counts[attr] += 1
            return 0, app.windows
        return original_read(element, attr, out)

    fake_ax.AXUIElementCopyAttributeValue = read_with_windows

    trees = adapter.get_browser_trees(42)

    assert len(trees[0].children) == adapter._BROWSER_MAX_ELEMENTS - 1
    assert len(trees[1].children) == 1
    assert fake_ax.batch_reads == adapter._BROWSER_MAX_ELEMENTS + 2


class FakeRunningApp:
    def __init__(self, pid: int, name: str, bundle_id: str, *, active: bool = False):
        self._pid = pid
        self._name = name
        self._bundle_id = bundle_id
        self._active = active

    def activationPolicy(self):
        return 0

    def processIdentifier(self):
        return self._pid

    def localizedName(self):
        return self._name

    def bundleIdentifier(self):
        return self._bundle_id

    def isActive(self):
        return self._active


def test_list_apps_uses_one_quartz_snapshot_instead_of_ax_round_trips():
    running_apps = [
        FakeRunningApp(11, "Safari", "com.apple.Safari", active=True),
        FakeRunningApp(22, "Firefox", "org.mozilla.firefox"),
    ]
    workspace = SimpleNamespace(runningApplications=lambda: running_apps)
    cocoa = SimpleNamespace(
        NSWorkspace=SimpleNamespace(sharedWorkspace=lambda: workspace),
        NSApplicationActivationPolicyRegular=0,
    )
    quartz = SimpleNamespace(
        kCGWindowListOptionAll=1,
        kCGNullWindowID=0,
        kCGWindowOwnerPID="pid",
        kCGWindowLayer="layer",
        kCGWindowName="name",
        kCGWindowBounds="bounds",
        CGWindowListCopyWindowInfo=lambda _options, _window: [
            {"pid": 11, "layer": 0, "name": "Agent Eyes docs", "bounds": {"Width": 900, "Height": 700}},
            {"pid": 11, "layer": 0, "name": "Agent Eyes docs", "bounds": {"Width": 900, "Height": 700}},
            {"pid": 11, "layer": 3, "name": "Popover", "bounds": {"Width": 300, "Height": 200}},
            {"pid": 22, "layer": 0, "name": "Issue tracker", "bounds": {"Width": 800, "Height": 600}},
        ],
    )
    app = FakeAXElement(1, "AXApplication")
    adapter, fake_ax = _adapter_with_ax(app)
    adapter._cocoa = cocoa
    adapter._quartz = quartz

    apps = adapter.list_apps()

    assert [(item.name, item.windows) for item in apps] == [
        ("Safari", ["Agent Eyes docs"]),
        ("Firefox", ["Issue tracker"]),
    ]
    assert fake_ax.read_counts == Counter()


def _convert_macos_attrs(attrs: dict):
    adapter = MacOSAdapter()
    adapter._ax = SimpleNamespace(
        AXUIElementCopyActionNames=lambda _element, _out: (0, []),
    )
    adapter._batch_read_attrs = lambda _element: attrs
    adapter.reset_ids()
    return adapter._element_to_ui(object(), 0, 0)


def test_secure_text_field_value_is_redacted_at_adapter_boundary():
    secret = "macos-secure-secret-b813"

    element = _convert_macos_attrs(
        {
            "AXRole": "AXTextField",
            "AXSubrole": "AXSecureTextField",
            "AXTitle": "Password",
            "AXValue": secret,
            "AXEnabled": True,
            "AXChildren": [],
        }
    )

    assert element is not None
    assert element.value == ""
    assert "secure" in element.states
    assert secret not in element.to_text()


def test_non_secure_text_field_value_is_preserved():
    element = _convert_macos_attrs(
        {
            "AXRole": "AXTextField",
            "AXSubrole": "AXStandardTextField",
            "AXTitle": "Search",
            "AXValue": "rollerblade",
            "AXEnabled": True,
            "AXChildren": [],
        }
    )

    assert element is not None
    assert element.value == "rollerblade"
    assert "secure" not in element.states


def test_native_identity_uses_core_foundation_equality():
    first_ref = object()
    second_ref = object()
    adapter = MacOSAdapter()
    adapter._ax = SimpleNamespace(CFEqual=lambda first, second: first is second)

    from agent_eyes.adapters.base import UIElement

    first = UIElement(id=1, role="textfield", platform_ref=first_ref)
    same = UIElement(id=2, role="textfield", platform_ref=first_ref)
    different = UIElement(id=3, role="textfield", platform_ref=second_ref)

    assert adapter.is_same_element(first, same) is True
    assert adapter.is_same_element(first, different) is False
