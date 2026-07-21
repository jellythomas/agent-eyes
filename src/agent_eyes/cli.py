"""Lightweight, model-independent command line entry point for Agent Eyes."""

from __future__ import annotations

import importlib
import os
import sys
from enum import IntEnum

from agent_eyes import __version__


_LAUNCHER_VERSION_CACHE: dict[tuple[str, int, int], bool] = {}


def __getattr__(name: str):
    """Keep test/integration patch points without loading runtime modules for --help."""
    if name in {"shutil", "subprocess"}:
        return importlib.import_module(name)
    raise AttributeError(name)


class ExitCode(IntEnum):
    OK = 0
    ERROR = 1
    USAGE = 2
    SETUP_REQUIRED = 3
    ACTION_REQUIRED = 4
    CANCELLED = 5


def _add_profile(parser) -> None:
    parser.add_argument(
        "--profile",
        choices=("standard", "full"),
        default="standard",
        help="Capability profile to verify (default: standard)",
    )


def _add_output(parser) -> None:
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")


def _add_mutation_controls(parser) -> None:
    parser.add_argument("--yes", action="store_true", help="Approve the displayed user-level plan")
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Never prompt; requires --yes to apply user-level changes",
    )
    parser.add_argument("--dry-run", action="store_true", help="Display the plan without changing anything")


def _add_clients(parser) -> None:
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--client",
        action="append",
        help="Configure one detected MCP client (repeatable)",
    )
    selection.add_argument(
        "--all-detected",
        action="store_true",
        help="Configure every detected MCP client",
    )


def build_parser():
    import argparse

    parser = argparse.ArgumentParser(
        prog="agent-eyes",
        description="Model-independent, native-first computer use over MCP",
    )
    parser.add_argument("--version", action="version", version=f"agent-eyes {__version__}")
    parser.set_defaults(handler=_run_serve)
    commands = parser.add_subparsers(dest="command")

    serve = commands.add_parser("serve", help="Run the MCP server over stdio")
    serve.add_argument(
        "--log-level",
        choices=("debug", "info", "warning", "error"),
        default="info",
    )
    serve.set_defaults(handler=_run_serve)

    doctor = commands.add_parser("doctor", help="Run read-only live readiness checks")
    _add_profile(doctor)
    _add_output(doctor)
    doctor.add_argument("--verbose", action="store_true", help="Show every capability check")
    doctor.add_argument("--refresh", action="store_true", help="Ignore the cached diagnostic snapshot")
    doctor.set_defaults(handler=_run_doctor)

    install = commands.add_parser("install", help="Install or repair runtime components")
    _add_profile(install)
    _add_output(install)
    _add_mutation_controls(install)
    install.add_argument("--repair", action="store_true", help="Force a persistent reinstall")
    install.set_defaults(handler=_run_install)

    init = commands.add_parser("init", help="Configure detected MCP clients")
    _add_output(init)
    _add_mutation_controls(init)
    _add_clients(init)
    init.set_defaults(handler=_run_init)

    setup = commands.add_parser("setup", help="Run guided install, init, and verification")
    _add_profile(setup)
    _add_output(setup)
    _add_mutation_controls(setup)
    _add_clients(setup)
    setup.add_argument("--repair", action="store_true", help="Reapply and reverify setup")
    setup.set_defaults(handler=_run_setup)
    return parser


def _status_exit(status: str) -> ExitCode:
    if status == "setup_required":
        return ExitCode.SETUP_REQUIRED
    if status == "permission_required":
        return ExitCode.ACTION_REQUIRED
    if status == "degraded":
        return ExitCode.ACTION_REQUIRED
    return ExitCode.OK


def _run_serve(args) -> int:
    import logging

    level_name = getattr(args, "log_level", "info").upper()
    logging.basicConfig(level=getattr(logging, level_name, logging.INFO))
    from agent_eyes.server import main as serve

    result = serve()
    return int(result) if isinstance(result, int) else int(ExitCode.OK)


def _run_doctor(args) -> int:
    import json

    from agent_eyes.setup.readiness import ReadinessStore, probe_current_readiness

    report = probe_current_readiness(profile=args.profile)
    ReadinessStore().save(report)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    else:
        print(report.to_text(verbose=args.verbose))
    return int(_status_exit(report.status.value))


def _confirm(prompt: str, *, approved: bool, non_interactive: bool) -> bool:
    if approved:
        return True
    if non_interactive or not sys.stdin.isatty():
        return False
    print(f"{prompt} [y/N] ", end="", file=sys.stderr, flush=True)
    answer = sys.stdin.readline().strip().lower()
    return answer in {"y", "yes"}


def _render_install_plan(plan) -> str:
    command = " ".join(plan.command)
    return f"- {plan.description}\n  Command: {command}"


def _emit_error(args, message: str, code: ExitCode) -> int:
    import json

    if getattr(args, "json", False):
        print(json.dumps({"status": "error", "error": message}, indent=2, sort_keys=True))
    else:
        print(f"ERROR: {message}", file=sys.stderr)
    return int(code)


def _is_transient_environment_launcher(executable) -> bool:
    """Return whether a PATH launcher belongs to the active virtual environment."""
    from pathlib import Path

    if sys.prefix == sys.base_prefix:
        return False
    environment_bin = Path(sys.prefix) / (
        "Scripts" if sys.platform == "win32" else "bin"
    )
    try:
        environment_bin = environment_bin.resolve()
        launcher = executable.resolve()
    except OSError:
        return False
    return executable.parent.resolve() == environment_bin or launcher.parent == environment_bin


def _persistent_executable():
    from pathlib import Path
    import shutil
    import subprocess

    candidates = []
    uv = shutil.which("uv")
    if uv:
        try:
            completed = subprocess.run(
                [uv, "tool", "dir", "--bin"],
                check=True,
                capture_output=True,
                text=True,
            )
            candidate = Path(completed.stdout.strip()) / (
                "agent-eyes.exe" if sys.platform == "win32" else "agent-eyes"
            )
            if candidate.exists():
                candidates.append(candidate.absolute())
        except (OSError, subprocess.SubprocessError):
            pass
    pipx_candidate = Path(
        os.environ.get("PIPX_BIN_DIR", str(Path.home() / ".local" / "bin"))
    ) / ("agent-eyes.exe" if sys.platform == "win32" else "agent-eyes")
    if pipx_candidate.exists():
        candidates.append(pipx_candidate.absolute())
    discovered = shutil.which("agent-eyes")
    if discovered:
        discovered_path = Path(discovered).absolute()
        if not _is_transient_environment_launcher(discovered_path):
            candidates.append(discovered_path)

    unique = list(dict.fromkeys(candidates))
    for candidate in unique:
        if _launcher_matches_current(candidate):
            return candidate
    return unique[0] if unique else None


def _launcher_matches_current(executable) -> bool:
    """Return whether a persistent launcher reports this exact Agent Eyes version."""
    import subprocess

    if executable is None or not executable.exists():
        return False
    try:
        stat = executable.stat()
    except OSError:
        return False
    cache_key = (str(executable), stat.st_mtime_ns, stat.st_size)
    cached = _LAUNCHER_VERSION_CACHE.get(cache_key)
    if cached is not None:
        return cached
    try:
        completed = subprocess.run(
            [str(executable), "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    matches = (
        completed.returncode == 0
        and completed.stdout.strip() == f"agent-eyes {__version__}"
    )
    _LAUNCHER_VERSION_CACHE[cache_key] = matches
    return matches


def _probe_persistent_readiness(executable, profile: str):
    """Ask the installed launcher to verify its own provider environment."""
    import json
    import subprocess

    from agent_eyes.setup.readiness import ReadinessReport, probe_current_readiness

    # Unit/integration callers may inject a launcher identity without a real
    # executable.  Real setup always verifies the executable before reaching
    # this path; keep the injected probe seam useful without spawning a bogus
    # file.
    if not executable.exists() or not os.access(executable, os.X_OK):
        return probe_current_readiness(
            persistent_executable=executable if executable.exists() else None,
            profile=profile,
        )

    completed = subprocess.run(
        [str(executable), "doctor", "--json", "--profile", profile],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    try:
        payload = json.loads(completed.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise RuntimeError(
            "Persistent launcher readiness check returned invalid JSON "
            f"(exit={completed.returncode}, "
            f"stdout_chars={len(completed.stdout or '')}, "
            f"stderr_chars={len(completed.stderr or '')})"
        ) from exc
    return ReadinessReport.from_dict(payload)


def _prepare_install(*, repair: bool):
    """Resolve a current launcher or a manager-specific install target and plan."""
    from agent_eyes.setup.install import (
        build_install_plan,
        resolve_persistent_executable,
    )

    existing = _persistent_executable()
    if not repair and _launcher_matches_current(existing):
        return existing, None

    plan = build_install_plan(
        version=__version__,
        force=repair or existing is not None,
    )
    executable = resolve_persistent_executable(plan.manager, plan.command[0])
    if not repair and _launcher_matches_current(executable):
        return executable, None
    if executable.exists() and "--force" not in plan.command:
        plan = build_install_plan(
            version=__version__,
            manager=plan.manager,
            manager_path=plan.command[0],
            force=True,
        )
    return executable, plan


def _run_install(args) -> int:
    import json
    import subprocess

    from agent_eyes.setup.install import apply_install_plan

    try:
        executable, plan = _prepare_install(repair=args.repair)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        return _emit_error(args, str(exc), ExitCode.SETUP_REQUIRED)

    if plan is None:
        payload = {
            "status": "already_current",
            "executable": str(executable),
            "install": None,
            "dry_run": args.dry_run,
        }
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(f"Agent Eyes {__version__} is already installed at {executable}.")
        return int(ExitCode.OK)

    if not args.json:
        print("Agent Eyes install plan:")
        print(_render_install_plan(plan))

    if args.dry_run:
        payload = {
            "status": "planned",
            "executable": str(executable),
            "install": {"plan": plan.to_dict(), "result": None},
            "dry_run": True,
        }
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        return int(ExitCode.OK)

    result = apply_install_plan(
        plan,
        consent=lambda _: _confirm(
            "Apply this user-level installation plan?",
            approved=args.yes,
            non_interactive=args.non_interactive,
        ),
    )
    payload = {
        "status": "installed",
        "executable": str(executable),
        "install": {"plan": plan.to_dict(), "result": result.to_dict()},
        "dry_run": False,
    }
    if result.cancelled:
        payload["status"] = "cancelled"
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print("Installation cancelled; no changes were made.", file=sys.stderr)
        return int(ExitCode.CANCELLED)
    if result.error:
        payload["status"] = "error"
        payload["error"] = result.error
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(f"ERROR: {result.error}", file=sys.stderr)
        return int(ExitCode.ERROR)
    if not _launcher_matches_current(executable):
        return _emit_error(
            args,
            f"Installation completed but {executable} does not report Agent Eyes {__version__}",
            ExitCode.ERROR,
        )
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"Persistent Agent Eyes installation completed at {executable}.")
    return int(ExitCode.OK)


def _client_targets(client_ids: list[str] | None) -> list[dict]:
    from agent_eyes.setup.scanner import _ai_tool_definitions, scan_ai_tools

    detected_ids = {tool["id"] for tool in scan_ai_tools()}
    selected = set(client_ids or detected_ids)
    definitions = {definition["id"]: definition for definition in _ai_tool_definitions()}
    unknown = selected - definitions.keys()
    if unknown:
        raise ValueError(f"Unknown MCP client(s): {', '.join(sorted(unknown))}")
    return [definitions[client_id] for client_id in sorted(selected)]


def _config_plan(targets: list[dict]) -> list[dict]:
    result = []
    for target in targets:
        location = target["config_locations"].get("global_mcp")
        if not location:
            continue
        config_format = str(location.get("format", "")).lower()
        if config_format not in {"json", "jsonc", "toml"}:
            raise ValueError(
                f"Unsupported MCP config format for {target['id']}: "
                f"{config_format or 'missing'}"
            )
        result.append(
            {
                "artifact": "mcp",
                "client": target["id"],
                "path": str(location["path"]),
                "servers_key": location["key"],
                "is_zed": target["id"] == "zed",
                "format": config_format,
            }
        )
    return result


def _skill_plan(targets: list[dict]) -> list[dict]:
    """Return canonical skill artifacts for clients that support skills."""
    from pathlib import Path

    result = []
    for target in targets:
        if not target.get("supports_skills", False):
            continue
        location = target["config_locations"].get("skills")
        if not location:
            raise ValueError(f"Skill directory is missing for {target['id']}")
        if location.get("type") != "directory":
            raise ValueError(f"Unsupported skill location for {target['id']}")
        skill_dir = Path(location["path"]) / "agent-eyes"
        result.append(
            {
                "artifact": "skill",
                "client": target["id"],
                "path": str(skill_dir / "SKILL.md"),
            }
        )
        if target["id"] == "codex":
            result.append(
                {
                    "artifact": "skill-metadata",
                    "client": target["id"],
                    "path": str(skill_dir / "agents" / "openai.yaml"),
                }
            )
    return result


def _client_change_plan(targets: list[dict]) -> list[dict]:
    return [*_config_plan(targets), *_skill_plan(targets)]


def _preflight_client_configs(
    config_plan: list[dict],
    executable,
):
    from pathlib import Path

    from agent_eyes.setup.configurator import preflight_mcp_file, preflight_text_file
    from agent_eyes.setup.templates.openai_skill import OPENAI_YAML
    from agent_eyes.setup.templates.skill import SKILL_MD

    prepared = []
    for item in config_plan:
        artifact = item.get("artifact", "mcp")
        if artifact == "mcp":
            plan = preflight_mcp_file(
                Path(item["path"]),
                servers_key=item["servers_key"],
                executable=executable,
                is_zed=item["is_zed"],
                config_format=item.get("format", "json"),
            )
        elif artifact == "skill":
            plan = preflight_text_file(
                Path(item["path"]),
                content=SKILL_MD,
                source_format="skill",
            )
        elif artifact == "skill-metadata":
            plan = preflight_text_file(
                Path(item["path"]),
                content=OPENAI_YAML,
                source_format="skill-metadata",
            )
        else:
            raise ValueError(f"Unsupported setup artifact: {artifact}")
        prepared.append((item, plan))
    return prepared


def _config_results(prepared, *, apply: bool) -> list[dict]:
    from agent_eyes.setup.configurator import apply_mcp_plans

    results = (
        apply_mcp_plans(tuple(plan for _, plan in prepared))
        if apply
        else (None,) * len(prepared)
    )
    changes = []
    for (item, plan), result in zip(prepared, results):
        changes.append(
            {
                "artifact": item.get("artifact", "mcp"),
                "client": item["client"],
                "path": plan.path,
                "changed": plan.changed,
                "applied": result.applied if result is not None else False,
                "backup": result.backup if result is not None else None,
            }
        )
    return changes


def _apply_client_configs(
    config_plan: list[dict],
    executable,
    *,
    dry_run: bool,
) -> list[dict]:
    prepared = _preflight_client_configs(config_plan, executable)
    return _config_results(prepared, apply=not dry_run)


def _run_init(args) -> int:
    import json

    executable = _persistent_executable()
    if not _launcher_matches_current(executable):
        return _emit_error(
            args,
            "A current persistent launcher was not found. Run agent-eyes setup.",
            ExitCode.SETUP_REQUIRED,
        )
    try:
        targets = _client_targets(args.client)
        plan = _client_change_plan(targets)
        prepared = _preflight_client_configs(plan, executable)
    except ValueError as exc:
        return _emit_error(args, str(exc), ExitCode.USAGE)
    except Exception as exc:
        return _emit_error(
            args,
            f"Client configuration preflight failed: {exc}",
            ExitCode.ERROR,
        )

    preview = _config_results(prepared, apply=False)
    payload = {
        "status": "planned" if args.dry_run else "pending",
        "executable": str(executable),
        "clients": plan,
        "changes": preview,
        "dry_run": args.dry_run,
    }
    if not args.json:
        print(
            f"Agent Eyes will configure {len(targets)} selected MCP client(s) "
            f"with {len(plan)} artifact(s):"
        )
        for item in plan:
            print(f"- {item['client']} {item['artifact']}: {item['path']}")
    if args.dry_run:
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        return int(ExitCode.OK)
    if not _confirm(
        "Apply these MCP configuration changes?",
        approved=args.yes,
        non_interactive=args.non_interactive,
    ):
        payload["status"] = "cancelled"
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        return int(ExitCode.CANCELLED)
    try:
        changes = _config_results(prepared, apply=True)
    except Exception as exc:
        return _emit_error(args, f"Client configuration failed: {exc}", ExitCode.ERROR)
    from agent_eyes.setup.state import mark_initialized

    mark_initialized(__version__, [target["id"] for target in targets], [])
    changed = sum(1 for item in changes if item["changed"])
    payload["status"] = "configured"
    payload["changes"] = changes
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(
            f"Updated {changed} MCP configuration/skill artifact(s). "
            "Restart changed clients to reconnect and reload skills."
        )
    return int(ExitCode.OK)


def _run_setup(args) -> int:
    import json
    import subprocess

    from agent_eyes.setup.install import apply_install_plan
    from agent_eyes.setup.readiness import (
        ReadinessStatus,
        ReadinessStore,
        probe_current_readiness,
    )

    try:
        executable, install_plan = _prepare_install(repair=args.repair)
        if install_plan is None:
            precheck = _probe_persistent_readiness(executable, args.profile)
        else:
            precheck = probe_current_readiness(
                persistent_executable=executable if executable.exists() else None,
                profile=args.profile,
            )
        if install_plan is None and precheck.status is ReadinessStatus.SETUP_REQUIRED:
            executable, install_plan = _prepare_install(repair=True)
        targets = _client_targets(args.client)
        client_plan = _client_change_plan(targets)
        prepared = _preflight_client_configs(client_plan, executable)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        return _emit_error(args, str(exc), ExitCode.SETUP_REQUIRED)
    except Exception as exc:
        return _emit_error(
            args,
            f"Client configuration preflight failed: {exc}",
            ExitCode.ERROR,
        )

    combined = {
        "status": "planned" if args.dry_run else "pending",
        "install": (
            {"plan": install_plan.to_dict(), "result": None}
            if install_plan is not None
            else None
        ),
        "executable": str(executable),
        "clients": client_plan,
        "changes": _config_results(prepared, apply=False),
        "precheck": precheck.to_dict(),
        "profile": args.profile,
        "dry_run": args.dry_run,
    }
    if not args.json:
        print("Agent Eyes guided setup plan:")
        if install_plan is None:
            print(f"- Keep current Agent Eyes {__version__} launcher: {executable}")
        else:
            print(_render_install_plan(install_plan))
        for item in client_plan:
            print(
                f"- Configure {item['client']} {item['artifact']}: "
                f"{item['path']}"
            )
        print("- Verify native accessibility, input, and permissions")

    if args.dry_run:
        if args.json:
            print(json.dumps(combined, indent=2, sort_keys=True))
        return int(ExitCode.OK)
    approved = _confirm(
        "Apply this user-level setup plan?",
        approved=args.yes,
        non_interactive=args.non_interactive,
    )
    if not approved:
        combined["status"] = "cancelled"
        if args.json:
            print(json.dumps(combined, indent=2, sort_keys=True))
        else:
            print("Setup cancelled; no changes were made.", file=sys.stderr)
        return int(ExitCode.CANCELLED)

    if install_plan is not None:
        install_result = apply_install_plan(
            install_plan,
            consent=lambda _: True,
        )
        combined["install"]["result"] = install_result.to_dict()
        if install_result.error or not install_result.applied:
            return _emit_error(
                args,
                install_result.error or "Installation did not complete",
                ExitCode.ERROR,
            )
        if not _launcher_matches_current(executable):
            return _emit_error(
                args,
                f"Installation completed but {executable} does not report Agent Eyes {__version__}",
                ExitCode.ERROR,
            )
    try:
        changes = _config_results(prepared, apply=True)
    except Exception as exc:
        return _emit_error(args, f"Client configuration failed: {exc}", ExitCode.ERROR)

    report = _probe_persistent_readiness(executable, args.profile)
    ReadinessStore().save(report)
    from agent_eyes.setup.state import mark_initialized

    if bool(getattr(report, "core_ready", False)):
        mark_initialized(__version__, [target["id"] for target in targets], [])
    combined["status"] = report.status.value
    combined["readiness"] = report.to_dict()
    combined["changes"] = changes
    if args.json:
        print(json.dumps(combined, indent=2, sort_keys=True))
    else:
        print(report.to_text(verbose=True))
        if any(item["changed"] for item in changes):
            print("Restart changed MCP clients so they use the persistent launcher.")
    return int(_status_exit(report.status.value))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    if args.command is None:
        args.command = "serve"
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
