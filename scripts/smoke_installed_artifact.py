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

from agent_eyes.setup.templates.skill import SKILL_MD


_CATALOG_LIMIT_BYTES = 16 * 1024
_OUTPUT_LIMITS = {"execute": 2 * 1024, "observe_target": 4 * 1024}


def _expected_tool_count(platform_name: str) -> int:
    return 30 if platform_name == "darwin" else 27


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
                compact_errors = {
                    "observe_target": await session.call_tool(
                        "observe_target", {"query": ""}
                    ),
                    "execute": await session.call_tool(
                        "execute", {"target": {}, "steps": []}
                    ),
                }

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
    tools_by_name = {tool.name: tool for tool in tools.tools}
    observe_schema = tools_by_name.get("observe_target")
    execute_schema = tools_by_name.get("execute")
    if observe_schema is None or execute_schema is None:
        raise RuntimeError("installed MCP transaction tools are missing")
    if "selectors" not in observe_schema.inputSchema.get("properties", {}):
        raise RuntimeError("installed observe_target schema is incomplete")
    execute_properties = execute_schema.inputSchema.get("properties", {})
    if not {"target", "steps"}.issubset(execute_properties):
        raise RuntimeError("installed execute schema is incomplete")
    catalog_bytes = len(
        json.dumps(
            [tool.model_dump(mode="json", exclude_none=True) for tool in tools.tools],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    if catalog_bytes > _CATALOG_LIMIT_BYTES:
        raise RuntimeError(
            f"installed MCP catalog exceeds {_CATALOG_LIMIT_BYTES} bytes: "
            f"{catalog_bytes}"
        )
    compact_error_bytes: dict[str, int] = {}
    for name, result in compact_errors.items():
        if not result.isError:
            raise RuntimeError(f"installed {name} accepted an invalid smoke request")
        rendered = "\n".join(
            item.text for item in result.content if hasattr(item, "text")
        )
        compact_error_bytes[name] = len(rendered.encode("utf-8"))
        if compact_error_bytes[name] > _OUTPUT_LIMITS[name]:
            raise RuntimeError(
                f"installed {name} error exceeds its output budget: "
                f"{compact_error_bytes[name]}"
            )
    return {
        "protocol_version": initialized.protocolVersion,
        "server_version": initialized.serverInfo.version,
        "tool_count": len(tool_names),
        "catalog_bytes": catalog_bytes,
        "compact_error_bytes": compact_error_bytes,
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
        required_skill_markers = (
            "For a known task, call `execute` once.",
            "use at most two normal-path calls: `observe_target`",
            "Never enter a repeated tree/find",
        )
        if any(marker not in SKILL_MD for marker in required_skill_markers):
            raise RuntimeError("installed Agent Eyes skill lacks the bounded fast path")

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
        "skill_fast_path": True,
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
