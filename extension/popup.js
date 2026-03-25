// popup.js — show native host connection status
const el = document.getElementById("status");

chrome.runtime.sendMessage({ action: "ping" }, (response) => {
  if (chrome.runtime.lastError || !response) {
    el.textContent = "Native host: not connected";
    el.className = "status err";
  } else {
    el.textContent = "Native host: connected";
    el.className = "status ok";
  }
});
