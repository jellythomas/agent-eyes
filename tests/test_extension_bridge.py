"""Tests for ExtensionBridge — Python side of the Chrome Native Messaging bridge."""
from __future__ import annotations

import json
import os
import struct
import sys
import unittest.mock as mock

import pytest

from agent_eyes.extension_bridge import ExtensionBridge


class TestWireProtocol:
    """Test the 4-byte LE length-prefix + UTF-8 JSON wire format."""

    def test_write_message_produces_length_prefix(self):
        payload = {"action": "ping"}
        data = ExtensionBridge.write_message(payload)
        length = struct.unpack("<I", data[:4])[0]
        body = json.loads(data[4:].decode("utf-8"))
        assert length == len(data) - 4
        assert body == payload

    def test_write_message_utf8_encoding(self):
        payload = {"text": "こんにちは"}
        data = ExtensionBridge.write_message(payload)
        body_bytes = data[4:]
        assert json.loads(body_bytes.decode("utf-8"))["text"] == "こんにちは"

    def test_read_message_round_trips(self):
        import io
        payload = {"id": 42, "result": [1, 2, 3]}
        wire = ExtensionBridge.write_message(payload)
        stream = io.BytesIO(wire)
        recovered = ExtensionBridge.read_message(stream)
        assert recovered == payload

    def test_read_message_empty_body_raises(self):
        import io
        # 4-byte header claiming 5 bytes but stream is empty after header
        stream = io.BytesIO(struct.pack("<I", 5))
        with pytest.raises((ValueError, EOFError, json.JSONDecodeError)):
            ExtensionBridge.read_message(stream)

    def test_write_message_large_payload(self):
        payload = {"data": "x" * 10_000}
        data = ExtensionBridge.write_message(payload)
        length = struct.unpack("<I", data[:4])[0]
        assert length == len(data) - 4
        recovered = json.loads(data[4:].decode("utf-8"))
        assert len(recovered["data"]) == 10_000


class TestManifestPath:
    """Test platform-specific native messaging manifest path detection."""

    def test_manifest_path_macos(self):
        bridge = ExtensionBridge()
        with mock.patch("sys.platform", "darwin"):
            path = bridge._get_manifest_path()
        assert "Library/Application Support/Google/Chrome/NativeMessagingHosts" in path
        assert path.endswith("com.agent_eyes.bridge.json")

    def test_manifest_path_linux(self):
        bridge = ExtensionBridge()
        with mock.patch("sys.platform", "linux"):
            path = bridge._get_manifest_path()
        assert ".config/google-chrome/NativeMessagingHosts" in path
        assert path.endswith("com.agent_eyes.bridge.json")

    def test_manifest_path_contains_host_name(self):
        bridge = ExtensionBridge()
        path = bridge._get_manifest_path()
        assert "com.agent_eyes.bridge" in path

    def test_manifest_path_is_absolute(self):
        bridge = ExtensionBridge()
        path = bridge._get_manifest_path()
        assert os.path.isabs(path)


class TestIsConnected:
    """Test is_connected property."""

    def test_is_connected_false_when_manifest_missing(self, tmp_path):
        bridge = ExtensionBridge()
        nonexistent = str(tmp_path / "does_not_exist.json")
        with mock.patch.object(bridge, "_get_manifest_path", return_value=nonexistent):
            assert bridge.is_connected is False

    def test_is_connected_true_when_manifest_present(self, tmp_path):
        manifest = tmp_path / "com.agent_eyes.bridge.json"
        manifest.write_text('{"name": "com.agent_eyes.bridge"}')
        bridge = ExtensionBridge()
        with mock.patch.object(bridge, "_get_manifest_path", return_value=str(manifest)):
            assert bridge.is_connected is True

    def test_is_connected_defaults_to_false(self):
        bridge = ExtensionBridge()
        # In a test environment there's almost certainly no manifest installed,
        # but we mock to be deterministic
        with mock.patch.object(bridge, "_get_manifest_path", return_value="/nonexistent/path.json"):
            assert bridge.is_connected is False


class TestMessageSerialization:
    """Test that send() serializes action + params correctly."""

    def test_send_builds_correct_structure(self):
        bridge = ExtensionBridge()
        # Capture what write_message receives
        captured = []

        def fake_write(payload):
            captured.append(payload)
            return b"\x00" * 4  # dummy bytes

        with mock.patch.object(ExtensionBridge, "write_message", staticmethod(fake_write)):
            # send() is async/sync depending on implementation; test the sync part
            msg = bridge._build_message("navigate", {"url": "https://example.com"})

        assert msg["action"] == "navigate"
        assert msg["params"] == {"url": "https://example.com"}
        assert "id" in msg

    def test_send_increments_message_id(self):
        bridge = ExtensionBridge()
        msg1 = bridge._build_message("list_tabs", {})
        msg2 = bridge._build_message("list_tabs", {})
        assert msg2["id"] > msg1["id"]
