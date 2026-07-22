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

    function isVisible(style) {
        return style.display !== 'none' && style.visibility !== 'hidden' && style.opacity !== '0';
    }

    function isSecure(el, style) {
        var type = (el.getAttribute('type') || '').toLowerCase();
        if (el.tagName === 'INPUT' && type === 'password') return true;
        var autocomplete = (el.getAttribute('autocomplete') || '').toLowerCase().split(/\s+/);
        if (autocomplete.indexOf('current-password') >= 0
                || autocomplete.indexOf('new-password') >= 0) return true;
        if (el.value === undefined && el.contentEditable !== 'true') return false;
        try {
            var textSecurity = style.webkitTextSecurity
                || style.getPropertyValue('-webkit-text-security');
            return Boolean(textSecurity && textSecurity !== 'none');
        } catch (_error) {
            return false;
        }
    }

    var nextId = 1;
    function walk(el, depth) {
        if (depth > maxDepth || nextId > 500) return null;
        if (el.offsetParent === null && el.tagName !== 'BODY') return null;
        var style = window.getComputedStyle(el);
        if (!isVisible(style)) return null;

        var rect = el.getBoundingClientRect();
        var secure = isSecure(el, style);
        var node = {
            id: nextId++,
            role: getRole(el),
            name: getName(el),
            bounds: [Math.round(rect.x), Math.round(rect.y),
                     Math.round(rect.width), Math.round(rect.height)],
            interactive: isInteractive(el),
            children: [],
        };

        if (secure) node.secure = true;
        if (!secure && el.value !== undefined && el.value !== '') node.value = String(el.value).substring(0, 200);
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

_SECURE_ROLE_KEYS = frozenset({
    "password",
    "passwordfield",
    "passwordtext",
    "securetext",
    "securetextfield",
})


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


def _tree_node_is_secure(node: dict) -> bool:
    role_key = "".join(
        character
        for character in str(node.get("role", "")).casefold()
        if character.isalnum()
    )
    return bool(node.get("secure")) or role_key in _SECURE_ROLE_KEYS


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
    is_secure = _dom_is_secure(node)
    # Priority: aria-label > alt > title > placeholder > value
    for priority_attr in ("aria-label", "alt", "title", "placeholder", "value"):
        if is_secure and priority_attr == "value":
            continue
        for i in range(0, len(attrs) - 1, 2):
            if str(attrs[i]).casefold() == priority_attr:
                return str(attrs[i + 1])
    if is_secure:
        return ""
    # Fall back to text content for text nodes
    node_value = node.get("nodeValue", "")
    if node_value:
        return node_value.strip()
    return ""


def _dom_is_secure(node: dict) -> bool:
    tag = str(node.get("nodeName", "")).casefold()
    attrs = node.get("attributes", [])
    attributes: dict[str, str] = {}
    for index in range(0, len(attrs) - 1, 2):
        attributes[str(attrs[index]).casefold()] = str(attrs[index + 1])

    if tag == "input" and attributes.get("type", "").casefold() == "password":
        return True

    autocomplete_tokens = attributes.get("autocomplete", "").casefold().split()
    if {"current-password", "new-password"}.intersection(autocomplete_tokens):
        return True

    style = attributes.get("style", "")
    text_security_value: str | None = None
    text_security_is_important = False
    for declaration in style.split(";"):
        property_name, separator, property_value = declaration.partition(":")
        if not separator or property_name.strip().casefold() != "-webkit-text-security":
            continue
        normalized_value = property_value.strip().casefold()
        is_important = normalized_value.endswith("!important")
        if is_important:
            normalized_value = normalized_value.removesuffix("!important").strip()
        if text_security_value is None or is_important or not text_security_is_important:
            text_security_value = normalized_value
            text_security_is_important = is_important
    return text_security_value is not None and text_security_value != "none"


def merge_pierced_nodes(pierced_nodes: list[dict], existing_node_ids: set[int]) -> list[dict]:
    """Find elements in pierced DOM that are NOT in the accessibility tree.

    These are shadow DOM elements that the AX tree missed.
    Returns list of dicts with role, name, nodeId for each shadow element.
    Only includes elements that have a meaningful role AND name.
    """
    shadow_elements = []
    for node in pierced_nodes:
        node_id = node.get("nodeId")
        backend_node_id = node.get("backendNodeId")
        identity = backend_node_id if backend_node_id is not None else node_id
        if identity in existing_node_ids:
            continue  # Already in AX tree

        role = _dom_to_role(node)
        name = _dom_to_name(node)

        if role and name:  # Only include meaningful elements
            actionable = (
                isinstance(backend_node_id, int)
                and not isinstance(backend_node_id, bool)
                and backend_node_id > 0
            )
            shadow_element = {
                "role": role,
                "name": name,
                "nodeId": node_id,
                "backendNodeId": backend_node_id,
                "actionable": actionable,
                "source": "shadow-dom",
            }
            if _dom_is_secure(node):
                shadow_element["secure"] = True
            shadow_elements.append(shadow_element)

    return shadow_elements


def _walk_format(node: dict, lines: list[str], depth: int) -> None:
    prefix = "  " * depth
    el_id = node.get("id", "")
    role = node.get("role", "")
    name = node.get("name", "")
    value = node.get("value", "")
    secure = _tree_node_is_secure(node)
    interactive = node.get("interactive", False)
    bounds = node.get("bounds", [])

    # Build description — always include bracket ID (el_id may be 0 for root)
    parts = [f"[{el_id}]", role]
    if name:
        parts.append(f'"{name}"')
    if value and not secure:
        parts.append(f"value={value!r}")
    if secure:
        parts.append("secure")
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
