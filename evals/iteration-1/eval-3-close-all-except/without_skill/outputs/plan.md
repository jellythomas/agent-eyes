# Plan: Close All Tabs Except the Currently Active One

## Approach

Without any skill guidance, I would use a combination of `eyes_list_chrome_tabs` to discover all tabs, `eyes_context` to identify the active tab, and `eyes_close_tab` to close each unwanted tab one by one.

## Tool Call Sequence

1. **`mcp__agent-eyes__eyes_context`** (no parameters)
   - **Reasoning:** Get the current state -- which app is frontmost, what window/tab is active. This tells me which tab the user is "currently reading" so I know which one to keep.

2. **`mcp__agent-eyes__eyes_list_chrome_tabs`** (no parameters)
   - **Reasoning:** Get the full list of all open Chrome tabs with their IDs, titles, and URLs. I need this to know how many tabs exist and their indices, so I can figure out which ones to close and which one to preserve.

3. **Cross-reference step (no tool call -- reasoning only)**
   - **Reasoning:** Compare the active tab from `eyes_context` (step 1) with the tab list from `eyes_list_chrome_tabs` (step 2). Identify the tab index of the currently active tab. All other tab indices are targets for closure.

4. **`mcp__agent-eyes__eyes_close_tab`** with `{"tab_index": <index>}` -- repeated for each tab to close, **starting from the highest index and working downward**
   - **Reasoning:** Close tabs from highest index to lowest to avoid index shifting issues. If the active tab is at index 3 and there are 10 tabs (indices 0-9), I would call:
     - `eyes_close_tab(tab_index=9)`
     - `eyes_close_tab(tab_index=8)`
     - `eyes_close_tab(tab_index=7)`
     - `eyes_close_tab(tab_index=6)`
     - `eyes_close_tab(tab_index=5)`
     - `eyes_close_tab(tab_index=4)`
     - `eyes_close_tab(tab_index=2)`
     - `eyes_close_tab(tab_index=1)`
     - `eyes_close_tab(tab_index=0)`
     - (skipping index 3, the active tab)

5. **`mcp__agent-eyes__eyes_list_chrome_tabs`** (no parameters)
   - **Reasoning:** Verify that only the intended tab remains open. Confirm the operation succeeded.

## Potential Issues

- **Index shifting:** Closing a tab changes the indices of tabs that come after it. By closing from highest to lowest index, I avoid this problem entirely.
- **Identifying the active tab:** `eyes_context` gives the frontmost app and window info, but it may not directly give a tab index. I would need to match the window title or URL from `eyes_context` against the tab list from `eyes_list_chrome_tabs`. If the match is ambiguous, the active tab (index 0 in `eyes_close_tab` default) is likely the current one -- but this is an assumption that could be wrong.
- **Multiple windows:** If Chrome has multiple windows, `eyes_list_chrome_tabs` may return tabs across all windows. The plan assumes a single window. Handling multiple windows would require additional logic.
- **Token cost:** With many tabs (e.g., 50+), this requires one `eyes_close_tab` call per tab, which is expensive in terms of tool calls and tokens.
