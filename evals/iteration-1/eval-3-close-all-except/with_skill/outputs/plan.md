# Plan: Close All Tabs Except the Currently Active One

## User Request
"I have too many tabs open, close everything except the tab I'm currently reading"

## Analysis

This is a **destructive action** (closing tabs loses state). The Safety Protocol requires:
- Listing all tabs before closing any
- Matching tabs by title, not guessing indexes
- Re-reading state between each close (because closing tab N shifts all indexes above N)

The "tab I'm currently reading" means the currently active tab in Chrome. We need to identify it, then close every other tab one at a time, re-listing between each close.

---

## Exact Sequence of Tool Calls

### Step 1: Orient -- know what app/window is active
**Tool:** `mcp__agent-eyes__eyes_context`
**Parameters:** *(none)*
**Reasoning:** Gate step per the Standard Workflow. Confirms Chrome is the active app and we are operating on the correct window. If Chrome is not active, we would need to focus it first with `eyes_app`.

### Step 2: List all open Chrome tabs
**Tool:** `mcp__agent-eyes__eyes_list_chrome_tabs`
**Parameters:** *(none)*
**Reasoning:** Safety Protocol for `eyes_close_tab` requires calling `eyes_list_chrome_tabs` first. This gives us every tab's title, URL, and index. We identify which tab is marked as active -- that is "the tab I'm currently reading" and must be preserved.

### Step 3: Confirm with the user
**Action:** Present the list of tabs to the user. State which tab will be kept (the active one) and list all tabs that will be closed. Ask for confirmation before proceeding.
**Reasoning:** Closing many tabs is highly destructive and irreversible. The user should see exactly what will be lost. Example message: "I see 12 tabs open. I will keep 'How to Cook Pasta - WikiHow' (the active tab) and close the other 11. Proceed?"

### Step 4: Close the first non-active tab
**Tool:** `mcp__agent-eyes__eyes_close_tab`
**Parameters:** `{ "title": "<exact title of first non-active tab from Step 2>" }`
**Reasoning:** Use `title` parameter (preferred over index per Safety Protocol) to target the specific tab. Title matching is safer because indexes shift as tabs close.

### Step 5: Re-list tabs after first close
**Tool:** `mcp__agent-eyes__eyes_list_chrome_tabs`
**Parameters:** *(none)*
**Reasoning:** Safety Protocol says "do not chain destructive actions without re-reading state between them (closing tab N shifts all indexes above N)." We must re-list to confirm the correct tab was closed and to get the updated tab list before the next close.

### Step 6: Close the next non-active tab
**Tool:** `mcp__agent-eyes__eyes_close_tab`
**Parameters:** `{ "title": "<exact title of next non-active tab from Step 5>" }`
**Reasoning:** Same as Step 4, targeting the next tab to close using its title from the refreshed list.

### Steps 7-N: Repeat Steps 5-6 for each remaining non-active tab
For every remaining tab that is not the one being kept:
1. `mcp__agent-eyes__eyes_list_chrome_tabs` -- re-read state
2. `mcp__agent-eyes__eyes_close_tab` with `{ "title": "<exact title>" }` -- close one tab

This alternating pattern continues until only the active tab remains.

### Final Step: Verify
**Tool:** `mcp__agent-eyes__eyes_list_chrome_tabs`
**Parameters:** *(none)*
**Reasoning:** Confirm only the originally-active tab remains open. Report the result to the user.

---

## Key Safety Considerations

1. **Always close by title, never by index** -- indexes shift after every close.
2. **Re-list between every close** -- never assume the tab list is unchanged after a destructive action.
3. **Confirm before starting** -- the user said "close everything except," but we still present the plan so they can catch mistakes (e.g., they may have forgotten about an important tab).
4. **Read every `eyes_close_tab` response** -- verify the correct tab was closed before proceeding.
5. **If a title is ambiguous** (e.g., two tabs with the same title), fall back to matching by URL or ask the user which one to keep/close.
