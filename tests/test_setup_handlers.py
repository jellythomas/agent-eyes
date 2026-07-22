from __future__ import annotations

import json

import pytest

from agent_eyes.setup import handlers


def test_setup_scan_recommends_coexistence_instead_of_competitor_removal(monkeypatch):
    ai_tools = [{"id": "cursor", "name": "Cursor"}]
    report = {
        "summary": {"total_competitors": 1},
        "by_tool": {
            "cursor": {
                "agent_eyes_status": {},
                "mcp_competitors": [{"competitor_id": "playwright-mcp"}],
            }
        },
        "by_category": {
            "browser_automation": [
                {"id": "playwright-mcp", "name": "Playwright MCP"}
            ]
        },
        "by_competitor": {
            "playwright-mcp": {
                "found_in": [{"tool_name": "Cursor"}],
            }
        },
    }
    monkeypatch.setattr(handlers, "scan_ai_tools", lambda: ai_tools)
    monkeypatch.setattr(handlers, "scan_competitors", lambda _tools: report)
    monkeypatch.setattr(
        handlers,
        "CATEGORIES",
        {"browser_automation": "Browser Automation"},
    )

    rendered = handlers.handle_setup()

    assert "coexist" in rendered.lower()
    assert "ALL of the above" not in rendered
    assert "Keep competitors?" not in rendered
    assert "replace_competitors: []" in rendered
    machine = json.loads(rendered.split("--- MACHINE-READABLE DATA ---\n", 1)[1])
    assert machine["defaults"]["replace_ids"] == []
    assert machine["defaults"]["replace"] == "keep"


def test_setup_apply_decline_does_not_apply_or_mark_initialized(monkeypatch):
    applied = []
    marked = []
    monkeypatch.setattr(handlers, "scan_ai_tools", lambda: [])
    monkeypatch.setattr(handlers, "scan_competitors", lambda _tools: {})

    def apply_setup(**kwargs):
        applied.append(kwargs)
        return {
            "changes": [],
            "warnings": [],
            "backups_dir": "/unused",
            "applied": False,
            "cancelled": True,
            "dry_run": False,
        }

    monkeypatch.setattr(handlers, "apply_setup", apply_setup)
    monkeypatch.setattr(handlers, "mark_initialized", lambda *args, **kwargs: marked.append((args, kwargs)))

    rendered = handlers.handle_setup_apply(
        {
            "configure_tools": ["cursor"],
            "replace_competitors": ["playwright-mcp"],
            "approved": False,
        }
    )

    assert applied == []
    assert marked == []
    assert "cancelled" in rendered.lower()


@pytest.mark.parametrize("approved", [None, "false", 1])
def test_setup_apply_requires_explicit_boolean_approval(monkeypatch, approved):
    monkeypatch.setattr(
        handlers,
        "apply_setup",
        lambda **_kwargs: pytest.fail("setup must not run without explicit approval"),
    )
    monkeypatch.setattr(
        handlers,
        "mark_initialized",
        lambda *_args, **_kwargs: pytest.fail("unapproved setup must not be persisted"),
    )

    args = {"configure_tools": ["cursor"]}
    if approved is not None:
        args["approved"] = approved

    rendered = handlers.handle_setup_apply(args)

    assert "approval" in rendered.lower()
    assert "no changes" in rendered.lower()


def test_setup_apply_marks_only_clients_with_a_real_config_target(monkeypatch):
    marked = []
    monkeypatch.setattr(handlers, "scan_ai_tools", lambda: [])
    monkeypatch.setattr(handlers, "scan_competitors", lambda _tools: {})
    monkeypatch.setattr(
        handlers,
        "apply_setup",
        lambda **_kwargs: {
            "changes": [
                {
                    "tool": "cursor",
                    "action": "mcp_entry_unchanged",
                    "detail": "already current",
                }
            ],
            "warnings": ["claude-desktop has no project target"],
            "backups_dir": "/unused",
            "applied": False,
            "cancelled": False,
            "dry_run": False,
        },
    )
    monkeypatch.setattr(
        handlers,
        "mark_initialized",
        lambda *args, **kwargs: marked.append((args, kwargs)),
    )

    handlers.handle_setup_apply(
        {
            "configure_tools": ["cursor", "claude-desktop"],
            "approved": True,
        }
    )

    assert marked == [
        (
            (),
            {
                "version": handlers.__version__,
                "tools_configured": ["cursor"],
                "competitors_replaced": [],
            },
        )
    ]
