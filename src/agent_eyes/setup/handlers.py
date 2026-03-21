"""Setup handlers — MCP tool implementations for eyes_setup and eyes_setup_apply."""

import json

from .scanner import scan_ai_tools, scan_competitors
from .state import is_first_run, mark_initialized
from .competitors import CATEGORIES
from .configurator import apply_setup


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

    # Add "All" and "None" options
    all_letter = chr(65 + option_idx)
    none_letter = chr(66 + option_idx)
    lines.append(f"\n    {all_letter}. ALL of the above")
    lines.append(f"    {none_letter}. None (just add agent-eyes, keep existing)")

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

    # ── Next Steps
    lines.append("")
    lines.append("=" * 60)
    lines.append("  Next Steps")
    lines.append("=" * 60)
    lines.append("")
    lines.append("Present the options above to the user and ask:")
    lines.append("")
    lines.append("1. Which competing tools to REPLACE with agent-eyes?")
    lines.append("   (Select from the lettered options above)")
    lines.append("")
    lines.append("2. Which AI tools to CONFIGURE agent-eyes in?")
    lines.append("   (Select from the tool IDs listed above)")
    lines.append("")
    lines.append("3. Installation level: 'global' or 'project'?")
    lines.append("")
    lines.append("Then call eyes_setup_apply with the user's choices:")
    lines.append("  replace_competitors: [list of competitor IDs]")
    lines.append("  configure_tools: [list of AI tool IDs]")
    lines.append("  level: 'global' or 'project'")
    lines.append("")
    lines.append("IMPORTANT: Warn the user that this will modify config files.")
    lines.append("All changes are backed up automatically.")

    # Embed machine-readable data for the AI agent
    lines.append("")
    lines.append("--- MACHINE-READABLE DATA ---")
    lines.append(json.dumps({
        "ai_tools": [{"id": t["id"], "name": t["name"]} for t in ai_tools],
        "competitors": competitor_list,
        "scan_report_summary": scan_report["summary"],
    }, indent=2))

    return "\n".join(lines)


def handle_setup_apply(args: dict) -> str:
    """Apply agent-eyes configuration based on user selections."""
    replace_competitors = args.get("replace_competitors", [])
    configure_tools = args.get("configure_tools", [])
    level = args.get("level", "global")

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
    )

    # Mark as initialized
    mark_initialized(
        version="0.3.0",
        tools_configured=configure_tools,
        competitors_replaced=replace_competitors,
    )

    # Format response
    lines = []
    lines.append("=" * 60)
    lines.append("  agent-eyes setup — Changes Applied")
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
    lines.append("Setup complete! Restart your AI tool for changes to take effect.")

    return "\n".join(lines)
