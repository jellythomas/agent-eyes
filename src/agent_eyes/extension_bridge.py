"""Chrome Extension bridge — Python side of the Native Messaging protocol.

The Chrome extension (extension/background.js) connects to this host via
Chrome's Native Messaging API.  Messages are framed with a 4-byte
little-endian length prefix followed by a UTF-8-encoded JSON body.

Architecture:
  Chrome Extension  ←──────────────────────────────────→  Python (this module)
  background.js          Native Messaging stdio pipe       ExtensionBridge

This module provides:
  - ExtensionBridge  — high-level send/receive interface
  - Wire-protocol helpers (read_message / write_message)
  - Platform path detection for the native messaging manifest
"""
from __future__ import annotations

import asyncio
import json
import os
import struct
import sys
from io import RawIOBase
from typing import IO, Any

# Host name must match the manifest filename and the Chrome allowlist
_HOST_NAME = "com.agent_eyes.bridge"


class ExtensionBridge:
    """Python-side bridge to the agent-eyes Chrome Extension.

    Responsibilities:
      - Detect whether the native messaging manifest is installed (is_connected)
      - Provide the wire-protocol helpers (read_message / write_message)
      - Build and track outgoing messages (_build_message)
      - send() — write a command and await the matching response Future

    The actual stdio communication is handled by the native host process that
    Chrome spawns.  This class is used *inside* that process to speak the
    protocol, and also from the MCP server to check installation status.
    """

    def __init__(self) -> None:
        self._next_id: int = 1
        self._pending: dict[int, asyncio.Future] = {}

    # ── Installation check ─────────────────────────────────────────

    @property
    def is_connected(self) -> bool:
        """True if the native messaging host manifest is installed on disk.

        Chrome looks for the manifest at a platform-specific location.
        If the file exists we consider the bridge "installed" (connected).
        The extension itself may or may not be loaded in Chrome, but the
        manifest is the necessary prerequisite.
        """
        return os.path.isfile(self._get_manifest_path())

    def _get_manifest_path(self) -> str:
        """Return the platform-specific path for the native messaging manifest.

        Chrome reads the manifest from:
          macOS:  ~/Library/Application Support/Google/Chrome/NativeMessagingHosts/
          Linux:  ~/.config/google-chrome/NativeMessagingHosts/
          Win32:  HKCU\\Software\\Google\\Chrome\\NativeMessagingHosts\\<name>
                  (registry — not a file path; we return a sentinel on Windows)
        """
        home = os.path.expanduser("~")
        filename = f"{_HOST_NAME}.json"

        platform = sys.platform

        if platform == "darwin":
            base = os.path.join(
                home,
                "Library",
                "Application Support",
                "Google",
                "Chrome",
                "NativeMessagingHosts",
            )
        elif platform == "linux":
            base = os.path.join(home, ".config", "google-chrome", "NativeMessagingHosts")
        else:
            # Windows uses the registry; return a non-existent path so
            # is_connected returns False rather than raising.
            base = os.path.join(home, "AppData", "Local", "Google", "Chrome",
                                "User Data", "NativeMessagingHosts")

        return os.path.join(base, filename)

    # ── Wire protocol ──────────────────────────────────────────────

    @staticmethod
    def write_message(payload: dict[str, Any]) -> bytes:
        """Serialise *payload* as a Native Messaging frame.

        Frame format:
          [4 bytes LE uint32 = body length][UTF-8 JSON body]
        """
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        header = struct.pack("<I", len(body))
        return header + body

    @staticmethod
    def read_message(stream: IO[bytes]) -> dict[str, Any]:
        """Read one Native Messaging frame from *stream*.

        Raises:
          EOFError        if the stream is closed before a complete frame.
          ValueError      if the length header is missing / truncated.
          json.JSONDecodeError  if the body is not valid JSON.
        """
        header = stream.read(4)
        if not header:
            raise EOFError("Stream closed (no length header)")
        if len(header) < 4:
            raise ValueError(f"Truncated length header: got {len(header)} bytes")

        length = struct.unpack("<I", header)[0]
        body = stream.read(length)

        if len(body) < length:
            raise EOFError(f"Truncated body: expected {length}, got {len(body)} bytes")

        return json.loads(body.decode("utf-8"))

    # ── High-level messaging ───────────────────────────────────────

    def _build_message(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        """Create a request envelope with a monotonically increasing ID."""
        msg_id = self._next_id
        self._next_id += 1
        return {"id": msg_id, "action": action, "params": params}

    def connect(self) -> bool:
        """Verify the extension bridge is reachable (manifest installed).

        Returns True if the native messaging manifest exists, False otherwise.
        This is a lightweight check — it does not open any sockets.
        """
        return self.is_connected

    async def send(self, action: str, params: dict[str, Any] | None = None) -> Any:
        """Send *action* with *params* and await the matching response.

        This method is intended for use inside the native host process where
        stdin/stdout are connected to Chrome.  It writes the framed message
        to stdout and registers a Future that will be resolved when the
        matching response arrives via the dispatch loop.

        In the current MVP the server only *checks* is_connected rather than
        sending live messages, so this is provided for future use.

        Raises:
          RuntimeError  if the extension bridge is not connected.
        """
        if not self.is_connected:
            raise RuntimeError(
                "Extension bridge not connected. "
                "Run extension/install.sh to install the native messaging host."
            )

        msg = self._build_message(action, params or {})
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[msg["id"]] = fut

        # Write to stdout (the native messaging channel)
        frame = self.write_message(msg)
        sys.stdout.buffer.write(frame)
        sys.stdout.buffer.flush()

        return await fut

    def dispatch_response(self, response: dict[str, Any]) -> None:
        """Resolve the pending Future for *response["id"]*.

        Call this from the message-reading loop when Chrome sends a reply.
        """
        msg_id = response.get("id")
        fut = self._pending.pop(msg_id, None)
        if fut is None or fut.done():
            return

        if "error" in response:
            fut.set_exception(RuntimeError(response["error"]))
        else:
            fut.set_result(response.get("result"))
