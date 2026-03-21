# Plan: "go to the issues page on my github tab"

## Analysis

The user wants to navigate to the Issues page on an already-open GitHub tab in Chrome. This is a **navigation action** (destructive -- it changes the current page), so the Safety Protocol requires verifying which tab we are about to navigate before acting.

## Exact Sequence of Tool Calls

### Step 1: Orient -- know what app/window is active

**Tool:** `mcp__agent-eyes__eyes_context`
**Parameters:** *(none)*

**Reasoning:** This is the mandatory gate step from the Standard Workflow. We need to confirm Chrome is the active app and understand the current state before doing anything. If Chrome is not the active app, we would need to focus it first.

---

### Step 2: List Chrome tabs -- find the GitHub tab

**Tool:** `mcp__agent-eyes__eyes_list_chrome_tabs`
**Parameters:** *(none)*

**Reasoning:** The Safety Protocol for navigation requires calling `eyes_list_chrome_tabs` before `eyes_navigate` to confirm which tab we are about to change. The user said "my github tab," so we need to find a tab whose title or URL contains "github.com" and identify its index. We must not guess the index.

---

### Step 3: Navigate the GitHub tab to its Issues page

**Tool:** `mcp__agent-eyes__eyes_navigate`
**Parameters:**
- `url`: The issues URL derived from the GitHub tab's URL found in Step 2. For example, if the tab URL is `https://github.com/mekari/mcp-servers/task-ninja`, navigate to `https://github.com/mekari/mcp-servers/task-ninja/issues`. If the tab is already on a repo page, append `/issues` to the repo root URL.
- `tab_index`: The index of the GitHub tab identified in Step 2.

**Reasoning:** We now know which tab is the GitHub tab (from Step 2) and can construct the Issues URL from the repo URL. We use `tab_index` to target the correct tab. The Safety Protocol is satisfied because we verified the tab identity in Step 2.

---

### Step 4: Verify -- confirm the Issues page loaded

**Tool:** `mcp__agent-eyes__eyes_wait_for`
**Parameters:**
- `text`: `"Issues"` (or a similar element expected on the Issues page)
- `timeout`: `5000`

**Reasoning:** The Standard Workflow requires a verification step after acting. We wait for the Issues page heading/tab to appear, confirming navigation succeeded.

---

## Safety Protocol Compliance

| Requirement | How satisfied |
|---|---|
| Orient before acting | Step 1: `eyes_context` |
| List tabs before navigating | Step 2: `eyes_list_chrome_tabs` |
| Verify tab identity before navigating | Step 3 uses the index confirmed in Step 2 |
| Do not assume state | Every step reads state before acting on it |
| Verify result after action | Step 4: `eyes_wait_for` |
