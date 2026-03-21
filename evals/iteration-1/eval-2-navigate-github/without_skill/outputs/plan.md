# Plan: Navigate to GitHub Issues Page

**User request:** "go to the issues page on my github tab"

## Tool Call Sequence

### 1. `mcp__agent-eyes__eyes_list_chrome_tabs`

**Reasoning:** The user says "my github tab", implying they already have a GitHub tab open in Chrome. I need to find which tab is the GitHub one so I can switch to it or interact with it. This tool lists all open Chrome tabs, letting me identify the correct one.

**Parameters:** (none expected)

### 2. `mcp__agent-eyes__eyes_click` (on the GitHub tab)

**Reasoning:** Once I identify the GitHub tab from the list, I need to switch to it by clicking on it or activating it. Alternatively, I might use `eyes_navigate` if the tab is already focused, but first I need to make sure the correct tab is active.

**Parameters:** Depends on the tab identifier returned from step 1 -- would click the tab matching a GitHub URL pattern.

### 3. `mcp__agent-eyes__eyes_get_web_tree`

**Reasoning:** After switching to the GitHub tab, I need to read the current page structure to understand what is on screen and find the "Issues" navigation link. The web tree will show me the accessibility tree of the page, including navigation elements.

**Parameters:** (none expected, or possibly a selector scope)

### 4. `mcp__agent-eyes__eyes_click` (on the "Issues" tab/link)

**Reasoning:** From the web tree, I would locate the "Issues" link or tab in GitHub's repository navigation bar. GitHub repos typically have a tab bar with "Code", "Issues", "Pull requests", etc. I would click the element matching "Issues".

**Parameters:** The identifier/selector for the "Issues" link element found in the web tree (e.g., a text match like "Issues" or an accessibility tree node ID).

### 5. `mcp__agent-eyes__eyes_get_web_tree`

**Reasoning:** Verify that the navigation was successful and the Issues page has loaded. Confirm the page now shows the issues list.

**Parameters:** (none expected)

## Summary

Total calls: 5 tool calls across 3 distinct tools.

The approach is: discover tabs -> activate the GitHub tab -> read the page -> click "Issues" -> verify result.
