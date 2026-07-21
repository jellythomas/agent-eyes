"""Setup handlers — MCP tool implementations for eyes_setup and eyes_setup_apply."""

import json

from agent_eyes import __version__

from .configurator import apply_setup
from .competitors import CATEGORIES
from .scanner import scan_ai_tools, scan_competitors
from .state import mark_initialized


def handle_setup() -> str:
    """Scan for AI tools and competing MCP servers."""
    ai_tools = scan_ai_tools()
    scan_report = scan_competitors(ai_tools)

    lines = []
    lines.append("=" * 60)
    lines.append("  agent-eyes setup — Smart Scan Report")
    lines.append("=" * 60)
    lines.append("")

    # ── AI Tools Found
    lines.append(f"AI Tools Detected: {len(ai_tools)}")
    lines.append("-" * 40)
    for tool in ai_tools:
        status = scan_report["by_tool"].get(tool["id"], {})
        ae = status.get("agent_eyes_status", {})
        ae_label = ""
        if ae.get("mcp_configured"):
            ae_label = " [agent-eyes: configured]"
        elif ae.get("skill_installed"):
            ae_label = " [agent-eyes: skill only]"
        comps = len(status.get("mcp_competitors", []))
        lines.append(f"  [{tool['id']}] {tool['name']}{ae_label}")
        if comps:
            lines.append(f"    -> {comps} competing MCP server(s) found")

    lines.append("")

    # ── Competitors by Category
    total = scan_report["summary"]["total_competitors"]
    if total == 0:
        lines.append("No competing MCP servers found. You're all set!")
        lines.append("")
        lines.append("To add agent-eyes to your AI tools, call eyes_setup_apply with:")
        lines.append("  configure_tools: [list of tool IDs from above]")
        lines.append("  replace_competitors: []")
        lines.append("")
        lines.append("--- MACHINE-READABLE DATA ---")
        lines.append(json.dumps({
            "ai_tools": [{"id": t["id"], "name": t["name"]} for t in ai_tools],
            "competitors": [],
            "scan_report_summary": scan_report["summary"],
        }, indent=2))
        return "\n".join(lines)

    lines.append(f"Competing MCP Servers Found: {total}")
    lines.append("-" * 40)

    option_idx = 0
    competitor_list = []

    for cat_id, cat_name in CATEGORIES.items():
        cat_comps = scan_report["by_category"].get(cat_id, [])
        if not cat_comps:
            continue
        lines.append(f"\n  {cat_name}:")
        for comp in cat_comps:
            option_idx += 1
            letter = chr(64 + option_idx)  # A, B, C, ...
            found_in = scan_report["by_competitor"].get(comp["id"], {})
            locations = found_in.get("found_in", [])
            loc_names = [f["tool_name"] for f in locations]
            lines.append(
                f"    {letter}. {comp['name']} "
                f"(found in: {', '.join(loc_names)})"
            )
            competitor_list.append({
                "option": letter,
                "id": comp["id"],
                "name": comp["name"],
                "found_in": loc_names,
            })

    # ── Claude Code specific: skills and agents
    cc_report = scan_report["by_tool"].get("claude-code", {})
    skill_comps = cc_report.get("skill_competitors", [])
    agent_refs = cc_report.get("agent_refs", [])

    if skill_comps or agent_refs:
        lines.append("")
        lines.append("Claude Code Details:")
        lines.append("-" * 40)
        if skill_comps:
            lines.append("  Competing skills:")
            for s in skill_comps:
                lines.append(f"    - {s['competitor_name']} ({s['path']})")
        if agent_refs:
            lines.append("  Agent definitions with competitor tool refs:")
            agents_seen = set()
            for ref in agent_refs:
                if ref["agent_file"] not in agents_seen:
                    lines.append(
                        f"    - {ref['agent_file']} "
                        f"({ref['tool_refs_count']} refs to {ref['competitor_id']})"
                    )
                    agents_seen.add(ref["agent_file"])

    # Normal setup coexists with unrelated tools. Competitor removal is not a
    # setup side effect and therefore has no destructive default.
    all_tool_ids = [t["id"] for t in ai_tools]
    defaults = {
        "replace": "keep",
        "replace_ids": [],
        "tools": all_tool_ids,
        "level": "global",
    }

    lines.append("")
    lines.append("=" * 60)
    lines.append("  Quick Setup")
    lines.append("=" * 60)
    lines.append("")
    lines.append(
        "Normal setup will coexist with detected tools; it will not remove MCP "
        "entries, disable skills, or rewrite agent instructions."
    )
    lines.append("")
    lines.append("IMPORTANT: Present setup choices to the user using the BEST available UI.")
    lines.append("")
    lines.append("OPTION 1 — Native multi-choice (Claude Code):")
    lines.append("If the AskUserQuestion tool is available, use it to present 2 questions:")
    lines.append("")
    lines.append("  Question 1: \"Which AI tools should agent-eyes be configured in?\"")
    lines.append("    Header: \"Tools\"")
    lines.append("    Options:")
    lines.append(f"      - \"All ({len(ai_tools)} tools) (Recommended)\" → " + ", ".join(
        [t["name"] for t in ai_tools]
    ))
    if len(ai_tools) > 1:
        for t in ai_tools[:3]:
            lines.append(f"      - \"{t['name']} only\"")
    lines.append("")
    lines.append("  Question 2: \"What scope should the configuration be applied at?\"")
    lines.append("    Header: \"Level\"")
    lines.append("    Options:")
    lines.append("      - \"Global (Recommended)\" → available in all projects")
    lines.append("      - \"Project\" → only current project")
    lines.append("")
    lines.append("OPTION 2 — Text fallback (other AI tools):")
    lines.append("If AskUserQuestion is NOT available, present as compact text:")
    lines.append("─────────────────────────────────────────────")
    lines.append("  Configure in?         [All] " + " / ".join(
        [f"{t['id']}" for t in ai_tools]
    ))
    lines.append("  Level?                [global] / project")
    lines.append("─────────────────────────────────────────────")
    lines.append("")
    lines.append(
        "After user responds, call eyes_setup_apply with replace_competitors: [] "
        "and their client/scope choices. Set approved: true only after confirmation."
    )
    lines.append("All changes are backed up automatically.")

    # Embed machine-readable data for the AI agent
    lines.append("")
    lines.append("--- MACHINE-READABLE DATA ---")
    lines.append(json.dumps({
        "ai_tools": [{"id": t["id"], "name": t["name"]} for t in ai_tools],
        "competitors": competitor_list,
        "scan_report_summary": scan_report["summary"],
        "defaults": defaults,
    }, indent=2))

    return "\n".join(lines)


def handle_setup_apply(args: dict) -> str:
    """Apply agent-eyes configuration based on user selections."""
    replace_competitors = args.get("replace_competitors", [])
    configure_tools = args.get("configure_tools", [])
    level = args.get("level", "global")
    dry_run = bool(args.get("dry_run", False))
    approved = args.get("approved")

    if not dry_run and approved is not True:
        return (
            "Agent Eyes setup cancelled: explicit approval (approved: true) is required; "
            "no changes were made."
        )
    consent = approved is True or dry_run

    if not configure_tools:
        return "ERROR: configure_tools is required (list of AI tool IDs to configure)"

    # Re-scan to get fresh data for the configurator
    ai_tools = scan_ai_tools()
    scan_report = scan_competitors(ai_tools)

    # Apply changes
    result = apply_setup(
        replace_competitors=replace_competitors,
        configure_tools=configure_tools,
        level=level,
        scan_report=scan_report,
        dry_run=dry_run,
        consent=consent,
    )

    if result.get("cancelled"):
        return "Agent Eyes setup cancelled; no changes were made."

    if not result.get("dry_run"):
        configured_tools = [
            str(change["tool"])
            for change in result.get("changes", [])
            if change.get("tool")
        ]
        if configured_tools:
            mark_initialized(
                version=__version__,
                tools_configured=configured_tools,
                competitors_replaced=[],
            )

    # Format response
    lines = []
    lines.append("=" * 60)
    lines.append(
        "  agent-eyes setup — Preview"
        if result.get("dry_run")
        else "  agent-eyes setup — Changes Applied"
    )
    lines.append("=" * 60)
    lines.append("")

    if result["changes"]:
        lines.append(f"Changes made: {len(result['changes'])}")
        lines.append("-" * 40)
        for change in result["changes"]:
            tool_id = change.get("tool", "?")
            action = change.get("action", "?")
            detail = change.get("detail", "")
            backup = change.get("backup")
            lines.append(f"  [{tool_id}] {action}: {detail}")
            if backup:
                lines.append(f"    backup: {backup}")
    else:
        lines.append("No changes were needed.")

    if result["warnings"]:
        lines.append("")
        lines.append("Warnings:")
        for w in result["warnings"]:
            lines.append(f"  ! {w}")

    lines.append("")
    lines.append(f"All backups saved to: {result['backups_dir']}")
    lines.append("")
    if result.get("dry_run"):
        lines.append("Preview complete; no changes were made.")
    else:
        lines.append("Setup complete! Restart your AI tool for changes to take effect.")

    return "\n".join(lines)
