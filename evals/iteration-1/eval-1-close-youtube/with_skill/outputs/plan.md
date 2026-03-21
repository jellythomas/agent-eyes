# Plan: Close the YouTube tab

**User request:** "close the youtube tab that was opened earlier"

This is a **destructive action** (closing a tab loses state). The Safety Protocol in the skill mandates specific steps before executing `eyes_close_tab`.

---

## Step 1: Orient — `eyes_context`

**Tool:** `mcp__agent-eyes__eyes_context`
**Parameters:** _(none)_

**Reasoning:** The skill's Standard Workflow marks the orient step as a **gate** — it is not optional. Before any action, we must know which app and window is currently active. This confirms Chrome is running and tells us the current state. Per the Safety Protocol: "if you haven't called a read/list tool in this turn, you don't know the current state."

---

## Step 2: List all Chrome tabs — `eyes_list_chrome_tabs`

**Tool:** `mcp__agent-eyes__eyes_list_chrome_tabs`
**Parameters:** _(none)_

**Reasoning:** The Safety Protocol section "Before closing a tab" requires:
1. Call `eyes_list_chrome_tabs` to see every open tab with its title and URL.
2. Match the tab the user wants by title/URL — do not guess the index.

We need this to find which tab is YouTube. We look for a tab whose title contains "YouTube" or whose URL contains "youtube.com". We must **not** assume a tab index — indexes shift as tabs are opened and closed.

---

## Step 3: Close the YouTube tab by title — `eyes_close_tab`

**Tool:** `mcp__agent-eyes__eyes_close_tab`
**Parameters:**
- `title`: The exact title of the YouTube tab as returned by Step 2 (e.g., `"YouTube"` or whatever the full title is from the listing)

**Reasoning:** The Safety Protocol states: "Use the `title` parameter (preferred) or the verified `tab_index`." Title matching is preferred over index-based targeting because titles are human-readable and indexes shift. We use the exact title string observed in Step 2's output.

If multiple tabs match "YouTube" in their title, we would inform the user and ask which one to close before proceeding.

---

## Step 4: Verify — `eyes_list_chrome_tabs`

**Tool:** `mcp__agent-eyes__eyes_list_chrome_tabs`
**Parameters:** _(none)_

**Reasoning:** The Standard Workflow requires a verify step. We re-list tabs to confirm the YouTube tab is gone and no unintended tab was closed. We also read the response from Step 3 which confirms exactly which tab was closed (title and URL).

---

## Safety considerations applied

- **Did not skip orient:** Step 1 ensures we know the active app/window.
- **Did not guess tab index:** Step 2 lists all tabs; Step 3 uses title-based matching.
- **Used `title` over `tab_index`:** Per skill preference, title matching is safer.
- **Verified after action:** Step 4 confirms the correct tab was closed.
- **Single destructive action:** Only one tab close, so no need to re-read state between multiple destructive actions.
- **Ambiguity handling:** If multiple YouTube tabs exist, the plan calls for asking the user to disambiguate rather than guessing.
