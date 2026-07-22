from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from agent_eyes.cli import build_parser
from agent_eyes.input_validation import validate_tool_arguments
from agent_eyes.server import TOOLS
from agent_eyes.setup.scanner import _ai_tool_definitions


ROOT = Path(__file__).parents[1]
CLI_REFERENCE = ROOT / "docs" / "api" / "agent-eyes-cli.md"
MCP_REFERENCE = ROOT / "docs" / "api" / "mcp-tools.md"
README = ROOT / "README.md"


def _subparsers(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    action = next(
        candidate
        for candidate in parser._actions
        if isinstance(candidate, argparse._SubParsersAction)
    )
    return action.choices


def _tool_sections(content: str) -> dict[str, str]:
    tools_content = content.split("## Tools", maxsplit=1)[1]
    matches = list(re.finditer(r"^### `([^`]+)`[^\n]*$", tools_content, re.MULTILINE))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(tools_content)
        sections[match.group(1)] = tools_content[match.start() : end]
    return sections


def test_cli_reference_covers_every_command_option_and_client_id():
    content = CLI_REFERENCE.read_text(encoding="utf-8")
    parser = build_parser()

    for option in parser._option_string_actions:
        assert f"`{option}`" in content

    for name, command_parser in _subparsers(parser).items():
        assert f"## `agent-eyes {name}`" in content
        for option in command_parser._option_string_actions:
            assert f"`{option}`" in content

    for definition in _ai_tool_definitions():
        assert f"`{definition['id']}`" in content


def test_mcp_reference_covers_effective_tools_properties_and_valid_examples():
    content = MCP_REFERENCE.read_text(encoding="utf-8")
    sections = _tool_sections(content)

    for tool in TOOLS:
        assert tool.name in sections
        section = sections[tool.name]
        for contract_label in ("Returns", "Errors", "Side effects"):
            assert f"**{contract_label}:**" in section, (
                f"{tool.name} must document {contract_label.lower()}"
            )
        for property_name in tool.inputSchema.get("properties", {}):
            assert f"`{property_name}`" in section

        examples = re.findall(r"```json\n(.*?)\n```", section, re.DOTALL)
        assert examples, f"{tool.name} must include a JSON argument example"
        for raw_example in examples:
            arguments = json.loads(raw_example)
            validate_tool_arguments(tool.inputSchema, arguments)


def test_local_documentation_links_resolve():
    for path in (README, CLI_REFERENCE, MCP_REFERENCE):
        content = path.read_text(encoding="utf-8")
        for raw_target in re.findall(r"(?<!!)\[[^\]]+\]\(([^)]+)\)", content):
            target = raw_target.strip("<>").split("#", maxsplit=1)[0]
            if not target or "://" in target or target.startswith("mailto:"):
                continue
            resolved = (path.parent / target).resolve()
            assert resolved.exists(), f"broken local link in {path}: {raw_target}"
