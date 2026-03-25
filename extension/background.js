/**
 * agent-eyes Chrome Extension — Background Service Worker
 *
 * Connects to the agent-eyes Python server via Chrome Native Messaging.
 * The Python server sends commands here; we execute them using Chrome APIs
 * and send results back — no debug bar, no --remote-debugging-port required.
 *
 * Message protocol (same as Native Messaging wire format):
 *   Python → Extension: { id, action, params }
 *   Extension → Python: { id, result } or { id, error }
 *
 * Supported actions:
 *   list_tabs        — return all open tabs
 *   navigate         — navigate a tab to a URL
 *   new_tab          — open a new tab (optionally with a URL)
 *   close_tab        — close a tab by index or tabId
 *   execute_script   — inject and run JS in a tab, return the result
 *   get_ax_tree      — inject AX tree script and return structured result
 */

const NATIVE_HOST = "com.agent_eyes.bridge";
const RECONNECT_DELAY_MS = 2000;

let nativePort = null;
let reconnectTimer = null;

// ── Connection management ──────────────────────────────────────────

function connect() {
  if (nativePort) return;

  try {
    nativePort = chrome.runtime.connectNative(NATIVE_HOST);
  } catch (err) {
    console.error("[agent-eyes] Failed to connect to native host:", err);
    scheduleReconnect();
    return;
  }

  nativePort.onMessage.addListener(handleNativeMessage);

  nativePort.onDisconnect.addListener(() => {
    const err = chrome.runtime.lastError;
    if (err) {
      console.warn("[agent-eyes] Native host disconnected:", err.message);
    }
    nativePort = null;
    scheduleReconnect();
  });

  console.log("[agent-eyes] Connected to native host:", NATIVE_HOST);
}

function scheduleReconnect() {
  if (reconnectTimer) return;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connect();
  }, RECONNECT_DELAY_MS);
}

// ── Message dispatch ───────────────────────────────────────────────

async function handleNativeMessage(message) {
  const { id, action, params = {} } = message;

  let result = null;
  let error = null;

  try {
    switch (action) {
      case "list_tabs":
        result = await handleListTabs(params);
        break;
      case "navigate":
        result = await handleNavigate(params);
        break;
      case "new_tab":
        result = await handleNewTab(params);
        break;
      case "close_tab":
        result = await handleCloseTab(params);
        break;
      case "execute_script":
        result = await handleExecuteScript(params);
        break;
      case "get_ax_tree":
        result = await handleGetAxTree(params);
        break;
      default:
        error = `Unknown action: ${action}`;
    }
  } catch (err) {
    error = err.message || String(err);
    console.error(`[agent-eyes] Error handling action '${action}':`, err);
  }

  sendResponse(id, result, error);
}

function sendResponse(id, result, error) {
  if (!nativePort) {
    console.warn("[agent-eyes] Cannot send response — not connected to native host");
    return;
  }
  const msg = error ? { id, error } : { id, result };
  nativePort.postMessage(msg);
}

// ── Action handlers ────────────────────────────────────────────────

/**
 * list_tabs — return all open tabs across all windows.
 * Returns an array of { tabId, index, windowId, url, title, active, status }.
 */
async function handleListTabs(_params) {
  const tabs = await chrome.tabs.query({});
  return tabs.map((tab, i) => ({
    tabId:    tab.id,
    index:    i,
    windowId: tab.windowId,
    url:      tab.url || "",
    title:    tab.title || "",
    active:   tab.active,
    status:   tab.status,
  }));
}

/**
 * navigate — load a URL in a tab.
 * params: { tabId?, tabIndex?, url }
 * If neither tabId nor tabIndex is given, uses the active tab.
 */
async function handleNavigate(params) {
  const { url } = params;
  if (!url) throw new Error("navigate: 'url' param is required");

  const tabId = await resolveTabId(params);
  await chrome.tabs.update(tabId, { url });

  // Wait for the tab to finish loading (up to 10 s)
  await waitForTabLoad(tabId, 10000);
  const tab = await chrome.tabs.get(tabId);
  return { tabId, url: tab.url, title: tab.title };
}

/**
 * new_tab — open a new tab, optionally with a URL.
 * params: { url? }
 */
async function handleNewTab(params) {
  const createProps = params.url ? { url: params.url } : {};
  const tab = await chrome.tabs.create(createProps);
  if (params.url) {
    await waitForTabLoad(tab.id, 10000);
    const updated = await chrome.tabs.get(tab.id);
    return { tabId: updated.id, url: updated.url, title: updated.title };
  }
  return { tabId: tab.id, url: tab.url || "", title: tab.title || "" };
}

/**
 * close_tab — close a tab.
 * params: { tabId?, tabIndex? }
 */
async function handleCloseTab(params) {
  const tabId = await resolveTabId(params);
  await chrome.tabs.remove(tabId);
  return { closed: tabId };
}

/**
 * execute_script — inject JS into a tab and return the result.
 * params: { tabId?, tabIndex?, code }
 * The 'code' string is wrapped in an IIFE and its return value is sent back.
 */
async function handleExecuteScript(params) {
  const { code } = params;
  if (!code) throw new Error("execute_script: 'code' param is required");

  const tabId = await resolveTabId(params);
  const results = await chrome.scripting.executeScript({
    target: { tabId },
    func: (jsCode) => {
      try {
        // eslint-disable-next-line no-new-func
        return Function(`"use strict"; return (${jsCode})`)();
      } catch (e) {
        return { __error: e.message };
      }
    },
    args: [code],
  });

  const value = results?.[0]?.result;
  if (value && typeof value === "object" && value.__error) {
    throw new Error(`Script error: ${value.__error}`);
  }
  return { result: value };
}

/**
 * get_ax_tree — inject the AX tree extraction script and return the tree.
 * params: { tabId?, tabIndex?, maxDepth? }
 */
async function handleGetAxTree(params) {
  const tabId = await resolveTabId(params);
  const maxDepth = params.maxDepth ?? 10;

  const results = await chrome.scripting.executeScript({
    target: { tabId },
    func: (depth) => {
      // Minimal inline AX tree walker using the Chrome Accessibility Object Model (AOM)
      // For a richer tree, the Python side injects the full js_bridge script.
      function walk(el, d) {
        if (!el || d > depth) return null;
        const node = {
          role:     el.role || "unknown",
          name:     el.name || "",
          value:    el.value ?? null,
          children: [],
        };
        for (const child of (el.children || [])) {
          const c = walk(child, d + 1);
          if (c) node.children.push(c);
        }
        return node;
      }
      // AOM is not universally available; fall back to document title + URL
      if (typeof AccessibilityNode !== "undefined" && document.accessibilityRoot) {
        return { tree: walk(document.accessibilityRoot, 0), method: "aom" };
      }
      // Lightweight DOM-based fallback
      return {
        tree: {
          role: "WebArea",
          name: document.title,
          value: null,
          url: window.location.href,
          children: [],
        },
        method: "dom-fallback",
      };
    },
    args: [maxDepth],
  });

  return results?.[0]?.result ?? { error: "No result from tab" };
}

// ── Utilities ──────────────────────────────────────────────────────

/**
 * Resolve a tabId from params.
 * Accepts: { tabId }, { tabIndex }, or falls back to the active tab.
 */
async function resolveTabId(params) {
  if (params.tabId != null) return params.tabId;

  if (params.tabIndex != null) {
    const tabs = await chrome.tabs.query({});
    const tab = tabs[params.tabIndex];
    if (!tab) throw new Error(`No tab at index ${params.tabIndex}`);
    return tab.id;
  }

  // Default: active tab in the focused window
  const [active] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (!active) throw new Error("No active tab found");
  return active.id;
}

/**
 * Wait for a tab to finish loading, with a timeout.
 */
function waitForTabLoad(tabId, timeoutMs) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      chrome.tabs.onUpdated.removeListener(listener);
      resolve(); // Timeout is non-fatal — tab may have loaded
    }, timeoutMs);

    function listener(updatedTabId, changeInfo) {
      if (updatedTabId === tabId && changeInfo.status === "complete") {
        clearTimeout(timer);
        chrome.tabs.onUpdated.removeListener(listener);
        resolve();
      }
    }

    chrome.tabs.onUpdated.addListener(listener);
  });
}

// ── Bootstrap ──────────────────────────────────────────────────────

// Connect on service worker startup
connect();

// Keep the service worker alive when the native port is active.
// (Service workers can be terminated after 30 s of inactivity.)
chrome.runtime.onMessage.addListener(() => {
  // Any message keeps the worker alive; return true to stay alive for async.
  return false;
});
