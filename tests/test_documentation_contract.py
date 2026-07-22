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
BENCHMARK_RESULTS_README = ROOT / "benchmarks" / "results" / "README.md"
RUNTIME_RESULT = (
    ROOT
    / "benchmarks"
    / "results"
    / "macos-arm64-py312-v0.9.0-runtime.json"
)
STARTUP_RESULT = (
    ROOT
    / "benchmarks"
    / "results"
    / "macos-arm64-py312-v0.9.0-startup.json"
)
BASELINE_RESULT = (
    ROOT
    / "benchmarks"
    / "baselines"
    / "macos-arm64-py312-pre-hardening.json"
)


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


def test_readme_benchmark_table_matches_checked_in_results():
    readme = README.read_text(encoding="utf-8")
    result_notes = BENCHMARK_RESULTS_README.read_text(encoding="utf-8")
    runtime = json.loads(RUNTIME_RESULT.read_text(encoding="utf-8"))
    startup = json.loads(STARTUP_RESULT.read_text(encoding="utf-8"))
    baseline = json.loads(BASELINE_RESULT.read_text(encoding="utf-8"))

    assert runtime["schema_version"] == 2
    assert runtime["correctness"]["fixed_orchestration_sleep_calls"] == 0
    assert runtime["correctness"]["singleflight_provider_calls"] == [1]
    assert runtime["environment"]["git_head"] in result_notes

    immediate_p95 = runtime["latency"]["immediate_event_completion"]["p95_ms"]
    formatting_p95 = runtime["formatting"]["1000"]["latency"]["p95_ms"]
    catalog_bytes = runtime["context"]["tools_list_compact_json_bytes"]
    import_p95 = startup["latency"]["server_import"]["p95_ms"]
    mcp_p95 = startup["latency"]["mcp_initialize_and_tools_list"]["p95_ms"]
    baseline_immediate = baseline["latency_ms"]["native_event_immediate_completion"][
        "p95"
    ]
    baseline_formatting = baseline["latency_ms"]["format_1000_browser_targets"][
        "p95"
    ]
    baseline_catalog = baseline["context_bytes"]["tools_list_compact_json"]

    expected_values = (
        f"{import_p95:.2f} ms",
        f"{mcp_p95:.2f} ms",
        f"{immediate_p95:.3f} ms",
        f"{formatting_p95:.3f} ms",
        f"{catalog_bytes:,}",
        f"{(baseline_immediate - immediate_p95) / baseline_immediate:.1%} lower",
        f"{(baseline_formatting - formatting_p95) / baseline_formatting:.1%} lower",
        f"{(baseline_catalog - catalog_bytes) / baseline_catalog:.1%} lower",
    )
    for value in expected_values:
        assert value in readme
