"""JavaScript-based accessibility tree builder.

Injected into web pages via chrome.scripting or AppleScript to build
an accessibility-like tree from the DOM — no CDP required.
"""
from __future__ import annotations

BUILD_AX_TREE_JS = r"""
(function buildAccessibilityTree(maxDepth) {
    var ROLE_MAP = {
        'A': 'link', 'BUTTON': 'button', 'INPUT': 'textbox',
        'SELECT': 'combobox', 'TEXTAREA': 'textbox', 'LABEL': 'label',
        'H1': 'heading', 'H2': 'heading', 'H3': 'heading',
        'H4': 'heading', 'H5': 'heading', 'H6': 'heading',
        'NAV': 'navigation', 'MAIN': 'main', 'ASIDE': 'complementary',
        'HEADER': 'banner', 'FOOTER': 'contentinfo', 'SECTION': 'region',
        'ARTICLE': 'article', 'FORM': 'form', 'TABLE': 'table',
        'IMG': 'img', 'DIALOG': 'dialog', 'UL': 'list', 'OL': 'list',
        'LI': 'listitem', 'DETAILS': 'group', 'SUMMARY': 'button',
    };

    function getRole(el) {
        return el.getAttribute('role') || ROLE_MAP[el.tagName] || el.tagName.toLowerCase();
    }

    function getName(el) {
        var label = el.getAttribute('aria-label')
            || el.getAttribute('title')
            || el.getAttribute('alt')
            || el.getAttribute('placeholder');
        if (label) return label.substring(0, 150);
        if (['A', 'BUTTON', 'LABEL', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6',
             'LI', 'SUMMARY', 'OPTION'].indexOf(el.tagName) >= 0) {
            var text = el.textContent || '';
            return text.trim().substring(0, 150);
        }
        return '';
    }

    function isInteractive(el) {
        var tag = el.tagName;
        if (['A', 'BUTTON', 'INPUT', 'SELECT', 'TEXTAREA', 'SUMMARY'].indexOf(tag) >= 0) return true;
        if (el.getAttribute('role')) return true;
        if (el.getAttribute('tabindex') !== null) return true;
        if (el.onclick || el.getAttribute('onclick')) return true;
        if (el.contentEditable === 'true') return true;
        return false;
    }

    function isVisible(el) {
        if (el.offsetParent === null && el.tagName !== 'BODY') return false;
        var style = window.getComputedStyle(el);
        return style.display !== 'none' && style.visibility !== 'hidden' && style.opacity !== '0';
    }

    var nextId = 1;
    function walk(el, depth) {
        if (depth > maxDepth) return null;
        if (!isVisible(el)) return null;

        var rect = el.getBoundingClientRect();
        var node = {
            id: nextId++,
            role: getRole(el),
            name: getName(el),
            bounds: [Math.round(rect.x), Math.round(rect.y),
                     Math.round(rect.width), Math.round(rect.height)],
            interactive: isInteractive(el),
            children: [],
        };

        if (el.value !== undefined && el.value !== '') node.value = String(el.value).substring(0, 200);
        if (el.checked !== undefined) node.checked = el.checked;
        if (el.disabled) node.disabled = true;
        if (el.getAttribute('aria-expanded')) node.expanded = el.getAttribute('aria-expanded') === 'true';

        for (var i = 0; i < el.children.length; i++) {
            var child = walk(el.children[i], depth + 1);
            if (child) node.children.push(child);
        }
        return node;
    }

    return walk(document.body, 0);
})
"""


def build_ax_tree_script(max_depth: int = 5) -> str:
    """Return JS that builds and returns a JSON accessibility tree."""
    return f"JSON.stringify(({BUILD_AX_TREE_JS})({max_depth}))"


def format_ax_tree(tree: dict | None, indent: int = 0) -> str:
    """Format a JS-built accessibility tree into a readable text representation."""
    if tree is None:
        return "No accessibility tree available (empty page or not loaded)."

    lines: list[str] = []
    _walk_format(tree, lines, indent)
    return "\n".join(lines) if lines else "No elements found."


def _dom_to_role(node: dict) -> str:
    """Infer accessibility role from DOM node."""
    # Check ARIA role attribute first
    attrs = node.get("attributes", [])
    for i in range(0, len(attrs) - 1, 2):
        if attrs[i] == "role":
            return attrs[i + 1]

    # Skip non-element nodes
    if node.get("nodeType", 1) != 1:
        return ""

    tag = node.get("nodeName", "").lower()

    # Handle input types
    if tag == "input":
        input_type = ""
        for i in range(0, len(attrs) - 1, 2):
            if attrs[i] == "type":
                input_type = attrs[i + 1]
                break
        input_role_map = {
            "checkbox": "checkbox", "radio": "radio",
            "range": "slider", "submit": "button",
            "button": "button", "reset": "button",
        }
        return input_role_map.get(input_type, "textbox")

    tag_role_map = {
        "a": "link", "button": "button", "select": "combobox",
        "textarea": "textarea", "img": "image", "nav": "navigation",
        "main": "main", "header": "banner", "footer": "contentinfo",
        "h1": "heading", "h2": "heading", "h3": "heading",
        "h4": "heading", "h5": "heading", "h6": "heading",
        "table": "table", "form": "form", "dialog": "dialog",
        "menu": "menu", "option": "option", "li": "listitem",
        "ul": "list", "ol": "list",
    }
    return tag_role_map.get(tag, "")


def _dom_to_name(node: dict) -> str:
    """Extract accessible name from DOM node."""
    attrs = node.get("attributes", [])
    # Priority: aria-label > alt > title > placeholder > value
    for priority_attr in ("aria-label", "alt", "title", "placeholder", "value"):
        for i in range(0, len(attrs) - 1, 2):
            if attrs[i] == priority_attr:
                return attrs[i + 1]
    # Fall back to text content for text nodes
    node_value = node.get("nodeValue", "")
    if node_value:
        return node_value.strip()
    return ""


def merge_pierced_nodes(pierced_nodes: list[dict], existing_node_ids: set[int]) -> list[dict]:
    """Find elements in pierced DOM that are NOT in the accessibility tree.

    These are shadow DOM elements that the AX tree missed.
    Returns list of dicts with role, name, nodeId for each shadow element.
    Only includes elements that have a meaningful role AND name.
    """
    shadow_elements = []
    for node in pierced_nodes:
        node_id = node.get("nodeId")
        if node_id in existing_node_ids:
            continue  # Already in AX tree

        role = _dom_to_role(node)
        name = _dom_to_name(node)

        if role and name:  # Only include meaningful elements
            shadow_elements.append({
                "role": role,
                "name": name,
                "nodeId": node_id,
                "source": "shadow-dom",
            })

    return shadow_elements


def _walk_format(node: dict, lines: list[str], depth: int) -> None:
    prefix = "  " * depth
    el_id = node.get("id", "")
    role = node.get("role", "")
    name = node.get("name", "")
    value = node.get("value", "")
    interactive = node.get("interactive", False)
    bounds = node.get("bounds", [])

    # Build description — always include bracket ID (el_id may be 0 for root)
    parts = [f"[{el_id}]", role]
    if name:
        parts.append(f'"{name}"')
    if value:
        parts.append(f"value={value!r}")
    if node.get("checked") is not None:
        parts.append("checked" if node["checked"] else "unchecked")
    if node.get("disabled"):
        parts.append("disabled")
    if node.get("expanded") is not None:
        parts.append("expanded" if node["expanded"] else "collapsed")
    if bounds and any(b != 0 for b in bounds):
        parts.append(f"({bounds[0]},{bounds[1]} {bounds[2]}x{bounds[3]})")
    if interactive:
        parts.append("*")

    lines.append(f"{prefix}{' '.join(p for p in parts if p)}")

    for child in node.get("children", []):
        _walk_format(child, lines, depth + 1)
