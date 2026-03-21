# agent-eyes Setup Wizard

Run the agent-eyes first-time setup to configure your AI tools and replace competing MCP servers.

## Instructions

1. Call the `mcp__agent-eyes__eyes_setup` tool to scan this machine for AI coding tools and competing MCP servers.

2. Read the scan results. The response contains:
   - Detected AI tools (Claude Code, VS Code, Cursor, etc.)
   - Competing MCP servers found (Playwright, Puppeteer, etc.)
   - Machine-readable JSON data with defaults

3. Present the setup choices to the user using the **AskUserQuestion** tool with these 3 questions:

   **Question 1** — "Which competing MCP servers should agent-eyes replace?"
   - Header: "Replace"
   - Build options from the scan results competitors list
   - Mark the "All" option as "(Recommended)"
   - Always include a "None — keep existing" option

   **Question 2** — "Which AI tools should agent-eyes be configured in?"
   - Header: "Tools"
   - Build options from the scan results ai_tools list
   - Mark "All" as "(Recommended)"

   **Question 3** — "What scope should the configuration be applied at?"
   - Header: "Level"
   - Options: "Global (Recommended)" and "Project"

4. After the user makes their selections, map their choices back to the machine-readable data:
   - `replace_competitors`: list of competitor IDs to replace (from the JSON data)
   - `configure_tools`: list of AI tool IDs to configure (from the JSON data)
   - `level`: "global" or "project"

5. Call `mcp__agent-eyes__eyes_setup_apply` with those parameters.

6. Report the results to the user — what was changed, where backups were saved.

## Important

- If AskUserQuestion is NOT available (non-Claude-Code environments), fall back to presenting the text-based choices from the scan report and ask the user to type their selections.
- Always call `eyes_setup` first — never skip the scan step.
- The scan does NOT make any changes. Only `eyes_setup_apply` modifies files.
