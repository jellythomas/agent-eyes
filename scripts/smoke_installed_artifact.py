#!/usr/bin/env python3
"""Smoke-test an installed Agent Eyes CLI and MCP server outside the source tree."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def _expected_tool_count(platform_name: str) -> int:
    return 28 if platform_name == "darwin" else 25


def _isolated_environment(root: Path) -> dict[str, str]:
    """Return an environment that cannot redirect Python imports from the wheel."""
    environment = os.environ.copy()
    environment.pop("PYTHONHOME", None)
    environment.pop("PYTHONPATH", None)
    environment["PYTHONNOUSERSITE"] = "1"
    environment["HOME"] = str(root / "home")
    environment["AGENT_EYES_STATE_DIR"] = str(root / "state")
    return environment


def _run_cli(
    executable: Path,
    arguments: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        [str(executable), *arguments],
        cwd=cwd,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"CLI smoke failed for {arguments!r} with exit {completed.returncode}: "
            f"{completed.stderr.strip()}"
        )
    return completed


async def _smoke_mcp(
    executable: Path,
    *,
    cwd: Path,
    environment: dict[str, str],
    expected_version: str,
) -> dict[str, Any]:
    parameters = StdioServerParameters(
        command=str(executable),
        args=["serve"],
        cwd=cwd,
        env=environment,
    )
    with open(os.devnull, "w", encoding="utf-8") as errlog:
        async with stdio_client(parameters, errlog=errlog) as (
            read_stream,
            write_stream,
        ):
            async with ClientSession(read_stream, write_stream) as session:
                initialized = await session.initialize()
                tools = await session.list_tools()
                status = await session.call_tool("status", {})

    tool_names = [tool.name for tool in tools.tools]
    expected_tool_count = _expected_tool_count(sys.platform)
    if initialized.serverInfo.name != "agent-eyes":
        raise RuntimeError(f"unexpected MCP server name: {initialized.serverInfo.name}")
    if initialized.serverInfo.version != expected_version:
        raise RuntimeError(
            f"unexpected MCP server version: {initialized.serverInfo.version}"
        )
    if len(tool_names) != expected_tool_count or len(set(tool_names)) != len(
        tool_names
    ):
        raise RuntimeError(
            f"unexpected MCP tool catalog: {len(tool_names)} tools on {sys.platform}"
        )
    if "status" not in tool_names or status.isError:
        raise RuntimeError("installed MCP status tool failed")
    return {
        "protocol_version": initialized.protocolVersion,
        "server_version": initialized.serverInfo.version,
        "tool_count": len(tool_names),
        "status_content_items": len(status.content),
    }


def smoke_installed_artifact(executable: Path, expected_version: str) -> dict[str, Any]:
    executable = executable.resolve(strict=True)
    if not executable.is_file():
        raise ValueError(f"executable is not a file: {executable}")

    with tempfile.TemporaryDirectory(prefix="agent-eyes-artifact-smoke-") as temporary:
        root = Path(temporary)
        environment = _isolated_environment(root)

        version = _run_cli(
            executable,
            ["--version"],
            cwd=root,
            environment=environment,
        ).stdout.strip()
        if version != f"agent-eyes {expected_version}":
            raise RuntimeError(f"unexpected CLI version: {version}")
        help_text = _run_cli(
            executable,
            ["--help"],
            cwd=root,
            environment=environment,
        ).stdout
        if "Model-independent, native-first computer use over MCP" not in help_text:
            raise RuntimeError("installed CLI help is incomplete")

        setup = _run_cli(
            executable,
            ["setup", "--dry-run", "--json"],
            cwd=root,
            environment=environment,
        )
        setup_payload = json.loads(setup.stdout)
        if (
            setup_payload.get("status") != "planned"
            or setup_payload.get("dry_run") is not True
        ):
            raise RuntimeError("installed setup dry-run returned an unexpected plan")
        if (root / "state").exists():
            raise RuntimeError("installed setup dry-run created readiness state")

        mcp = asyncio.run(
            asyncio.wait_for(
                _smoke_mcp(
                    executable,
                    cwd=root,
                    environment=environment,
                    expected_version=expected_version,
                ),
                timeout=30,
            )
        )
    return {
        "cli_version": version,
        "setup_status": setup_payload["status"],
        "mcp": mcp,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--expected-version", required=True)
    arguments = parser.parse_args()
    result = smoke_installed_artifact(arguments.executable, arguments.expected_version)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
