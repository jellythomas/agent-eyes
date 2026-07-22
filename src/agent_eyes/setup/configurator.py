"""Apply agent-eyes configuration to selected AI tools.

CRITICAL: This module modifies user config files. Every change:
1. Creates a backup first
2. Only touches the specific sections needed
3. Preserves all unrelated config
4. Reports exactly what changed
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

try:  # Python 3.11+
    import tomllib as _toml
except ModuleNotFoundError:  # pragma: no cover - exercised in the Python 3.10 matrix
    try:
        import tomli as _toml  # type: ignore[no-redef]
    except ModuleNotFoundError:  # pragma: no cover - explicit fail-closed path
        _toml = None  # type: ignore[assignment]

from .state import get_backups_dir, get_backups_path, setup_process_lock
from .templates.mcp_entry import (
    get_mcp_entry,
    get_mcp_entry_zed,
    get_agent_eyes_tools_list,
    COMPETITOR_TOOL_PATTERNS,
)
from .templates.skill import SKILL_MD
from .templates.claude_md import CLAUDE_MD_SECTION
from .scanner import _ai_tool_definitions


class InvalidConfigError(RuntimeError):
    """Raised when setup would otherwise overwrite an invalid user config."""


_ANY_CURRENT_CONTENT = object()


@dataclass(frozen=True)
class ConfigureResult:
    changed: bool
    applied: bool
    path: str
    backup: str | None = None


@dataclass(frozen=True)
class ConfigurePlan:
    """A validated, deterministic MCP config change that has not been applied."""

    changed: bool
    path: str
    write_path: str
    original_content: str | None
    rendered_content: str
    source_format: str
    anchor_path: str | None = None
    anchor_device: int | None = None
    anchor_inode: int | None = None


@dataclass(frozen=True)
class _JsonToken:
    kind: str
    value: str | None
    start: int
    end: int


@dataclass(frozen=True)
class _JsonProperty:
    key: str
    key_token: _JsonToken
    value_start: _JsonToken
    value_end: _JsonToken
    comma: _JsonToken | None


@dataclass(frozen=True)
class _JsonObject:
    opening: _JsonToken
    closing: _JsonToken
    properties: tuple[_JsonProperty, ...]


@dataclass(frozen=True)
class _TomlHeader:
    start: int
    path: tuple[str, ...]
    is_array: bool


def _backup(path: Path, backups_dir: Path | None = None) -> str | None:
    """Create a private, collision-free backup. Return its path or ``None``."""
    if not path.exists():
        return None
    backup_dir = backups_dir or get_backups_dir()
    if backup_dir.is_symlink():
        raise InvalidConfigError(f"Backup directory must not be a symlink: {backup_dir}")
    try:
        backup_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    except OSError as exc:
        raise InvalidConfigError(f"Unable to create backup directory: {backup_dir}") from exc
    if not backup_dir.is_dir():
        raise InvalidConfigError(f"Backup path is not a directory: {backup_dir}")
    try:
        backup_dir.chmod(0o700)
    except OSError as exc:
        raise InvalidConfigError(f"Unable to make backup directory private: {backup_dir}") from exc

    safe_base = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", path.name).strip(" .") or "config"
    digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:12]
    descriptor, backup_name = tempfile.mkstemp(
        prefix=f"{safe_base}.{digest}.",
        suffix=".bak",
        dir=backup_dir,
    )
    backup_path = Path(backup_name)
    try:
        with os.fdopen(descriptor, "wb") as destination, path.open("rb") as source:
            os.chmod(backup_path, 0o600)
            shutil.copyfileobj(source, destination)
            destination.flush()
            os.fsync(destination.fileno())
        _fsync_directory(backup_dir)
    except Exception:
        backup_path.unlink(missing_ok=True)
        raise
    return str(backup_path)


def _logical_path(path: Path) -> Path:
    return path.expanduser().absolute()


def _resolve_write_path(path: Path) -> Path:
    """Resolve a file symlink so atomic replacement updates its target."""
    if path.is_symlink():
        try:
            write_path = path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise InvalidConfigError(f"Config symlink target is unavailable: {path}") from exc
    else:
        try:
            # Canonicalize parent-directory symlinks even when the final config
            # does not exist yet. The logical path is retained for reporting.
            write_path = path.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise InvalidConfigError(f"Config path is unavailable: {path}") from exc
    if write_path.exists():
        try:
            mode = write_path.stat().st_mode
        except OSError as exc:
            raise InvalidConfigError(f"Unable to inspect config target: {path}") from exc
        if not stat.S_ISREG(mode):
            raise InvalidConfigError(f"Config target must be a regular file: {path}")
    return write_path


def _directory_anchor(parent: Path) -> tuple[Path, int, int]:
    """Pin the nearest existing directory used to reach a config parent."""
    candidate = parent
    while not candidate.exists():
        if candidate.parent == candidate:
            raise InvalidConfigError(f"Config parent is unavailable: {parent}")
        candidate = candidate.parent
    if candidate.is_symlink() or not candidate.is_dir():
        raise InvalidConfigError(f"Config parent must be a real directory: {candidate}")
    try:
        identity = candidate.stat()
    except OSError as exc:
        raise InvalidConfigError(f"Unable to inspect config parent: {candidate}") from exc
    return candidate, identity.st_dev, identity.st_ino


def _read_text(path: Path) -> str | None:
    if path.is_symlink() and not path.exists():
        raise InvalidConfigError(f"Config symlink target is unavailable: {path}")
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8", newline="") as stream:
            return stream.read()
    except (OSError, UnicodeError) as exc:
        raise InvalidConfigError(f"Unable to read config: {path}") from exc


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Non-standard JSON numeric constant: {value}")


def _strict_json_loads(source: str):
    return json.loads(source, parse_constant=_reject_json_constant)


def _read_json(path: Path) -> dict | None:
    """Read a JSON object and fail closed when existing data is invalid."""
    if not path.exists():
        return None
    try:
        data = _strict_json_loads(path.read_text())
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        raise InvalidConfigError(f"Invalid JSON config: {path}") from exc
    if not isinstance(data, dict):
        raise InvalidConfigError(f"Config root must be a JSON object: {path}")
    return data


def _assert_expected_content(path: Path, expected_content: str | None) -> None:
    """Fail closed when a concrete target no longer has the planned content."""
    if path.is_symlink():
        raise InvalidConfigError(
            f"Config target changed while setup was writing; refusing to overwrite: {path}"
        )
    current_content = _read_text(path)
    if current_content != expected_content:
        raise InvalidConfigError(
            f"Config changed while setup was writing; refusing to overwrite: {path}"
        )


def _replace_file(temporary: Path, path: Path) -> None:
    """Replace a file while retaining Windows target ACLs and attributes."""
    if os.name != "nt" or not path.exists():
        os.replace(temporary, path)
        return

    # ReplaceFileW preserves the replaced file's DACLs, encryption, compression,
    # named streams, and other Windows attributes. os.replace does not.
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    replace_file = kernel32.ReplaceFileW
    replace_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.LPVOID,
    )
    replace_file.restype = wintypes.BOOL
    if not replace_file(str(path), str(temporary), None, 0, None, None):
        raise ctypes.WinError(ctypes.get_last_error())


def _copy_macos_metadata(source: Path, destination: Path) -> None:
    """Preserve macOS ACLs, flags, modes, and xattrs with the system copier."""
    copier = Path("/bin/cp")
    if not copier.is_file():
        raise InvalidConfigError(f"Unable to preserve metadata for config: {source}")
    try:
        subprocess.run(
            [str(copier), "-p", str(source), str(destination)],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise InvalidConfigError(f"Unable to preserve metadata for config: {source}") from exc


def _copy_replacement_metadata(source: Path, destination: Path) -> None:
    """Preserve replace-sensitive metadata before the atomic swap."""
    if sys.platform == "darwin":
        _copy_macos_metadata(source, destination)
    else:
        shutil.copystat(source, destination, follow_symlinks=False)


def _open_pinned_parent(
    path: Path,
    *,
    anchor_path: str | None,
    anchor_device: int | None,
    anchor_inode: int | None,
) -> int:
    """Open/create a config parent through no-follow directory descriptors."""
    if anchor_path is None or anchor_device is None or anchor_inode is None:
        anchor, anchor_device, anchor_inode = _directory_anchor(path.parent)
    else:
        anchor = Path(anchor_path)
    try:
        relative_parts = path.parent.relative_to(anchor).parts
    except ValueError as exc:
        raise InvalidConfigError(f"Config parent changed since preflight: {path}") from exc

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(anchor, flags)
    except OSError as exc:
        raise InvalidConfigError(f"Config parent changed since preflight: {path}") from exc
    try:
        identity = os.fstat(descriptor)
        if (identity.st_dev, identity.st_ino) != (anchor_device, anchor_inode):
            raise InvalidConfigError(f"Config parent changed since preflight: {path}")
        for component in relative_parts:
            if component in {"", ".", ".."}:
                raise InvalidConfigError(f"Unsafe config parent component: {path}")
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                os.mkdir(component, mode=0o777, dir_fd=descriptor)
                child = os.open(component, flags, dir_fd=descriptor)
            except OSError as exc:
                raise InvalidConfigError(f"Config parent changed since preflight: {path}") from exc
            os.close(descriptor)
            descriptor = child
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _read_text_at(parent_descriptor: int, name: str, display_path: Path) -> str | None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise InvalidConfigError(f"Unable to inspect config target: {display_path}") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise InvalidConfigError(f"Config target must be a regular file: {display_path}")
        with os.fdopen(descriptor, "r", encoding="utf-8", newline="") as stream:
            descriptor = -1
            return stream.read()
    except (OSError, UnicodeError) as exc:
        raise InvalidConfigError(f"Unable to read config: {display_path}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _assert_expected_content_at(
    parent_descriptor: int,
    path: Path,
    expected_content: str | None,
) -> None:
    if _read_text_at(parent_descriptor, path.name, path) != expected_content:
        raise InvalidConfigError(
            f"Config changed while setup was writing; refusing to overwrite: {path}"
        )


def _path_matches_descriptor(path: Path, descriptor: int) -> bool:
    try:
        path_identity = path.stat(follow_symlinks=False)
        descriptor_identity = os.fstat(descriptor)
    except OSError:
        return False
    return (path_identity.st_dev, path_identity.st_ino) == (
        descriptor_identity.st_dev,
        descriptor_identity.st_ino,
    )


def _create_temporary_at(parent_descriptor: int, path: Path) -> tuple[int, str]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    for _ in range(100):
        name = f".{path.name}.{secrets.token_hex(12)}.tmp"
        try:
            return os.open(name, flags, 0o600, dir_fd=parent_descriptor), name
        except FileExistsError:
            continue
    raise InvalidConfigError(f"Unable to allocate a temporary config file: {path}")


def _atomic_write_posix(
    path: Path,
    content: str,
    *,
    expected_content: str | None | object,
    anchor_path: str | None,
    anchor_device: int | None,
    anchor_inode: int | None,
) -> None:
    parent_descriptor = _open_pinned_parent(
        path,
        anchor_path=anchor_path,
        anchor_device=anchor_device,
        anchor_inode=anchor_inode,
    )
    temporary_name: str | None = None
    try:
        current_content = _read_text_at(parent_descriptor, path.name, path)
        if (
            expected_content is not _ANY_CURRENT_CONTENT
            and current_content != expected_content
        ):
            raise InvalidConfigError(
                f"Config changed while setup was writing; refusing to overwrite: {path}"
            )
        descriptor, temporary_name = _create_temporary_at(parent_descriptor, path)
        temporary = path.parent / temporary_name
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
                if current_content is not None:
                    if not _path_matches_descriptor(temporary, stream.fileno()):
                        raise InvalidConfigError(
                            f"Config parent changed while setup was writing: {path}"
                        )
                    _copy_replacement_metadata(path, temporary)
                    if not _path_matches_descriptor(temporary, stream.fileno()):
                        raise InvalidConfigError(
                            f"Config parent changed while setup was writing: {path}"
                        )
                    os.ftruncate(stream.fileno(), 0)
                    stream.seek(0)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            if expected_content is not _ANY_CURRENT_CONTENT:
                _assert_expected_content_at(
                    parent_descriptor,
                    path,
                    expected_content,  # type: ignore[arg-type]
                )
            os.replace(
                temporary_name,
                path.name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
            )
            temporary_name = None
            try:
                os.fsync(parent_descriptor)
            except OSError:
                pass
        except Exception:
            raise
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name, dir_fd=parent_descriptor)
            except FileNotFoundError:
                pass
        os.close(parent_descriptor)


def _unlink_expected_posix(
    path: Path,
    expected_content: str,
    *,
    anchor_path: str | None,
    anchor_device: int | None,
    anchor_inode: int | None,
) -> None:
    parent_descriptor = _open_pinned_parent(
        path,
        anchor_path=anchor_path,
        anchor_device=anchor_device,
        anchor_inode=anchor_inode,
    )
    try:
        _assert_expected_content_at(parent_descriptor, path, expected_content)
        os.unlink(path.name, dir_fd=parent_descriptor)
        try:
            os.fsync(parent_descriptor)
        except OSError:
            pass
    finally:
        os.close(parent_descriptor)


def _atomic_write(
    path: Path,
    content: str,
    *,
    expected_content: str | None | object = _ANY_CURRENT_CONTENT,
    anchor_path: str | None = None,
    anchor_device: int | None = None,
    anchor_inode: int | None = None,
) -> None:
    """Atomically replace a concrete file path with UTF-8 text."""
    if os.name != "nt":
        _atomic_write_posix(
            path,
            content,
            expected_content=expected_content,
            anchor_path=anchor_path,
            anchor_device=anchor_device,
            anchor_inode=anchor_inode,
        )
        return
    if path.is_symlink():
        raise InvalidConfigError(f"Config target must not be a symlink: {path}")
    if path.exists() and not path.is_file():
        raise InvalidConfigError(f"Config target must be a regular file: {path}")
    if expected_content is not _ANY_CURRENT_CONTENT:
        _assert_expected_content(path, expected_content)  # type: ignore[arg-type]
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            if path.exists():
                # POSIX metadata is copied explicitly. Windows target ACLs and
                # attributes are retained by ReplaceFileW below.
                _copy_replacement_metadata(path, temporary)
                os.ftruncate(stream.fileno(), 0)
                stream.seek(0)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if expected_content is not _ANY_CURRENT_CONTENT:
            _assert_expected_content(path, expected_content)  # type: ignore[arg-type]
        _replace_file(temporary, path)
        _fsync_directory(path.parent)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _fsync_directory(directory: Path) -> None:
    """Persist a directory entry when the platform supports directory fsync."""
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _write_json(path: Path, data: dict) -> None:
    """Atomically write JSON without replacing a config symlink itself."""
    logical_path = _logical_path(path)
    _atomic_write(
        _resolve_write_path(logical_path),
        json.dumps(data, indent=2) + "\n",
    )


def _strip_jsonc_comments(source: str) -> str:
    characters = list(source)
    index = 0
    while index < len(source):
        if source[index] == '"':
            index += 1
            while index < len(source):
                if source[index] == "\\":
                    index += 2
                    continue
                if source[index] == '"':
                    index += 1
                    break
                index += 1
            continue
        if source.startswith("//", index):
            end = source.find("\n", index + 2)
            end = len(source) if end == -1 else end
            for position in range(index, end):
                if characters[position] != "\r":
                    characters[position] = " "
            index = end
            continue
        if source.startswith("/*", index):
            end = source.find("*/", index + 2)
            if end == -1:
                raise ValueError("unterminated block comment")
            for position in range(index, end + 2):
                if characters[position] not in "\r\n":
                    characters[position] = " "
            index = end + 2
            continue
        index += 1
    return "".join(characters)


def _remove_trailing_commas(source: str) -> str:
    characters = list(source)
    index = 0
    in_string = False
    while index < len(source):
        character = source[index]
        if in_string:
            if character == "\\":
                index += 2
                continue
            if character == '"':
                in_string = False
            index += 1
            continue
        if character == '"':
            in_string = True
        elif character == ",":
            lookahead = index + 1
            while lookahead < len(source) and source[lookahead].isspace():
                lookahead += 1
            if lookahead < len(source) and source[lookahead] in "}]":
                characters[index] = " "
        index += 1
    return "".join(characters)


def _load_jsonc(source: str) -> dict:
    normalized = _remove_trailing_commas(_strip_jsonc_comments(source))
    data = _strict_json_loads(normalized)
    if not isinstance(data, dict):
        raise ValueError("config root is not an object")
    return data


def _parse_config(
    source: str,
    *,
    path: Path,
    allow_jsonc: bool,
) -> tuple[dict, str]:
    try:
        data = _strict_json_loads(source)
        source_format = "json"
    except (json.JSONDecodeError, ValueError) as json_error:
        if not allow_jsonc:
            raise InvalidConfigError(f"Invalid JSON config: {path}") from json_error
        try:
            data = _load_jsonc(source)
        except (json.JSONDecodeError, ValueError) as jsonc_error:
            raise InvalidConfigError(
                f"Invalid JSON/JSONC config: {path}"
            ) from jsonc_error
        source_format = "jsonc"
    if not isinstance(data, dict):
        raise InvalidConfigError(f"Config root must be a JSON object: {path}")
    return data, source_format


def _tokenize_jsonc(source: str) -> list[_JsonToken]:
    tokens: list[_JsonToken] = []
    index = 0
    punctuation = "{}[]:,"
    while index < len(source):
        character = source[index]
        if character.isspace():
            index += 1
            continue
        if source.startswith("//", index):
            end = source.find("\n", index + 2)
            index = len(source) if end == -1 else end
            continue
        if source.startswith("/*", index):
            end = source.find("*/", index + 2)
            if end == -1:
                raise ValueError("unterminated block comment")
            index = end + 2
            continue
        if character == '"':
            end = index + 1
            while end < len(source):
                if source[end] == "\\":
                    end += 2
                    continue
                if source[end] == '"':
                    end += 1
                    break
                end += 1
            raw = source[index:end]
            tokens.append(_JsonToken("string", json.loads(raw), index, end))
            index = end
            continue
        if character in punctuation:
            tokens.append(_JsonToken(character, None, index, index + 1))
            index += 1
            continue
        end = index + 1
        while (
            end < len(source)
            and not source[end].isspace()
            and source[end] not in punctuation
            and not source.startswith("//", end)
            and not source.startswith("/*", end)
        ):
            end += 1
        tokens.append(_JsonToken("literal", source[index:end], index, end))
        index = end
    return tokens


def _skip_json_value(tokens: list[_JsonToken], index: int) -> int:
    token = tokens[index]
    if token.kind not in "{[":
        return index + 1
    expected = ["}" if token.kind == "{" else "]"]
    index += 1
    while index < len(tokens) and expected:
        kind = tokens[index].kind
        if kind in "{[":
            expected.append("}" if kind == "{" else "]")
        elif kind in "}]":
            if kind != expected.pop():
                raise ValueError("mismatched JSON container")
        index += 1
    if expected:
        raise ValueError("unterminated JSON container")
    return index


def _parse_json_object(tokens: list[_JsonToken], opening_index: int) -> _JsonObject:
    opening = tokens[opening_index]
    if opening.kind != "{":
        raise ValueError("JSON value is not an object")
    properties: list[_JsonProperty] = []
    index = opening_index + 1
    while index < len(tokens) and tokens[index].kind != "}":
        key_token = tokens[index]
        if key_token.kind != "string" or tokens[index + 1].kind != ":":
            raise ValueError("invalid JSON object property")
        value_index = index + 2
        after_value = _skip_json_value(tokens, value_index)
        comma = None
        if after_value < len(tokens) and tokens[after_value].kind == ",":
            comma = tokens[after_value]
            after_value += 1
        properties.append(
            _JsonProperty(
                key=str(key_token.value),
                key_token=key_token,
                value_start=tokens[value_index],
                value_end=tokens[after_value - (2 if comma else 1)],
                comma=comma,
            )
        )
        index = after_value
    if index >= len(tokens):
        raise ValueError("unterminated JSON object")
    return _JsonObject(opening, tokens[index], tuple(properties))


def _line_indent(source: str, position: int) -> str:
    line_start = source.rfind("\n", 0, position) + 1
    prefix = source[line_start:position]
    return prefix if not prefix.strip() else ""


def _indent_unit(source: str) -> str:
    for line in source.splitlines():
        indentation = line[: len(line) - len(line.lstrip())]
        if "\t" in indentation:
            return "\t"
    return "  "


def _format_property_value(value: dict, indentation: str, newline: str) -> str:
    lines = json.dumps(value, indent=2).splitlines()
    return lines[0] + "".join(newline + indentation + line for line in lines[1:])


def _insert_jsonc_property(
    source: str,
    object_info: _JsonObject,
    key: str,
    value: dict,
) -> str:
    opening = object_info.opening
    closing = object_info.closing
    newline = "\r\n" if "\r\n" in source else "\n"
    interior = source[opening.end:closing.start]
    multiline = "\n" in interior or "\r" in interior

    if object_info.properties:
        first_indent = _line_indent(source, object_info.properties[0].key_token.start)
        property_indent = first_indent or (_line_indent(source, closing.start) + _indent_unit(source))
        last_property = object_info.properties[-1]
        comma_position = last_property.value_end.end
        comma = "" if last_property.comma is not None else ","
    else:
        property_indent = _line_indent(source, closing.start) + _indent_unit(source)
        comma_position = closing.start
        comma = ""

    key_and_value = (
        f"{json.dumps(key)}: "
        f"{_format_property_value(value, property_indent, newline)}"
    )
    close_line_start = source.rfind("\n", 0, closing.start) + 1
    close_has_only_indent = not source[close_line_start:closing.start].strip()

    if multiline and close_has_only_indent:
        prefix = source[:comma_position] + comma + source[comma_position:close_line_start]
        if prefix and not prefix.endswith(("\n", "\r")):
            prefix += newline
        return prefix + property_indent + key_and_value + newline + source[close_line_start:]

    prefix = source[:comma_position] + comma + source[comma_position:closing.start]
    separator = "" if prefix.endswith((" ", "\t", "\n", "\r")) else " "
    return prefix + separator + key_and_value + source[closing.start:]


def _render_jsonc_change(
    source: str,
    *,
    servers_key: str,
    entry: dict,
) -> str:
    tokens = _tokenize_jsonc(source)
    root = _parse_json_object(tokens, 0)
    servers_property = next(
        (prop for prop in root.properties if prop.key == servers_key),
        None,
    )
    if servers_property is None:
        return _insert_jsonc_property(
            source,
            root,
            servers_key,
            {"agent-eyes": entry},
        )

    servers_opening_index = tokens.index(servers_property.value_start)
    servers = _parse_json_object(tokens, servers_opening_index)
    agent_property = next(
        (prop for prop in servers.properties if prop.key == "agent-eyes"),
        None,
    )
    if agent_property is None:
        return _insert_jsonc_property(source, servers, "agent-eyes", entry)

    indentation = _line_indent(source, agent_property.key_token.start)
    replacement = _format_property_value(
        entry,
        indentation,
        "\r\n" if "\r\n" in source else "\n",
    )
    return (
        source[:agent_property.value_start.start]
        + replacement
        + source[agent_property.value_end.end:]
    )


_TOML_MARKER = "__agent_eyes_setup_marker__"


def _load_toml(source: str, *, path: Path) -> dict:
    if _toml is None:
        raise InvalidConfigError(
            "TOML validation is unavailable on this Python runtime; refusing to "
            f"modify {path}"
        )
    try:
        data = _toml.loads(source)
    except ValueError as exc:
        raise InvalidConfigError(f"Invalid TOML config: {path}") from exc
    if not isinstance(data, dict):
        raise InvalidConfigError(f"Config root must be a TOML table: {path}")
    return data


def _find_toml_marker_path(value: object, prefix: tuple[str, ...] = ()) -> tuple[str, ...] | None:
    if isinstance(value, list):
        for nested in value:
            found = _find_toml_marker_path(nested, prefix)
            if found is not None:
                return found
        return None
    if not isinstance(value, dict):
        return None
    if value.get(_TOML_MARKER) is True:
        return prefix
    for key, nested in value.items():
        found = _find_toml_marker_path(nested, (*prefix, str(key)))
        if found is not None:
            return found
    return None


def _toml_header_path(header: str, *, path: Path) -> tuple[str, ...]:
    parsed = _load_toml(f"{header}\n{_TOML_MARKER} = true\n", path=path)
    found = _find_toml_marker_path(parsed)
    if found is None:
        raise InvalidConfigError(f"Unable to parse TOML table header in {path}")
    return found


def _toml_normal_line_starts(source: str) -> set[int]:
    """Return line offsets that begin outside TOML multiline strings."""
    starts: set[int] = set()
    state = "normal"
    offset = 0
    for line in source.splitlines(keepends=True):
        if state == "normal":
            starts.add(offset)
        index = 0
        while index < len(line):
            if state == "normal":
                if line.startswith('"""', index):
                    state = "multiline_basic"
                    index += 3
                    continue
                if line.startswith("'''", index):
                    state = "multiline_literal"
                    index += 3
                    continue
                character = line[index]
                if character == "#":
                    break
                if character == '"':
                    state = "basic"
                elif character == "'":
                    state = "literal"
                index += 1
                continue
            if state == "basic":
                if line[index] == "\\":
                    index += 2
                    continue
                if line[index] == '"':
                    state = "normal"
                index += 1
                continue
            if state == "literal":
                if line[index] == "'":
                    state = "normal"
                index += 1
                continue

            quote = '"' if state == "multiline_basic" else "'"
            if state == "multiline_basic" and line[index] == "\\":
                index += 2
                continue
            if line[index] == quote:
                end = index
                while end < len(line) and line[end] == quote:
                    end += 1
                if end - index >= 3:
                    state = "normal"
                index = end
                continue
            index += 1
        offset += len(line)
    return starts


def _without_toml_comment(line: str) -> str:
    state = "normal"
    index = 0
    while index < len(line):
        character = line[index]
        if state == "normal":
            if character == "#":
                return line[:index]
            if character == '"':
                state = "basic"
            elif character == "'":
                state = "literal"
        elif state == "basic":
            if character == "\\":
                index += 2
                continue
            if character == '"':
                state = "normal"
        elif character == "'":
            state = "normal"
        index += 1
    return line


def _toml_table_headers(source: str, *, path: Path) -> list[_TomlHeader]:
    normal_starts = _toml_normal_line_starts(source)
    headers: list[_TomlHeader] = []
    offset = 0
    for line in source.splitlines(keepends=True):
        if offset in normal_starts:
            candidate = _without_toml_comment(line).strip()
            if candidate.startswith("[") and candidate.endswith("]"):
                try:
                    header_path = _toml_header_path(candidate, path=path)
                except InvalidConfigError:
                    pass
                else:
                    headers.append(
                        _TomlHeader(
                            start=offset,
                            path=header_path,
                            is_array=candidate.startswith("[["),
                        )
                    )
        offset += len(line)
    return headers


def _toml_key(key: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_-]+", key):
        return key
    return json.dumps(key)


def _render_toml_entry(*, servers_key: str, entry: dict, newline: str) -> str:
    table_path = ".".join((_toml_key(servers_key), _toml_key("agent-eyes")))
    return (
        f"[{table_path}]{newline}"
        f"command = {json.dumps(entry['command'])}{newline}"
        f"args = {json.dumps(entry['args'])}{newline}"
    )


def _render_toml_change(
    source: str,
    *,
    path: Path,
    servers_key: str,
    entry: dict,
    existing_entry: object,
) -> str:
    target_path = (servers_key, "agent-eyes")
    headers = _toml_table_headers(source, path=path)
    targeted = [
        index
        for index, header in enumerate(headers)
        if header.path[: len(target_path)] == target_path
    ]
    if existing_entry is not None and not targeted:
        raise InvalidConfigError(
            "Existing Agent Eyes TOML entry must use table form for an exact safe update: "
            f"{path}"
        )

    if any(headers[index].is_array for index in targeted):
        raise InvalidConfigError(
            f"Agent Eyes TOML entry must not be an array table: {path}"
        )
    newline = "\r\n" if "\r\n" in source else "\n"
    replacement = _render_toml_entry(
        servers_key=servers_key,
        entry=entry,
        newline=newline,
    )
    if not targeted:
        separator = "" if not source or source.endswith(newline * 2) else newline
        if source and not source.endswith(("\n", "\r")):
            separator = newline * 2
        elif source:
            separator = newline
        return source + separator + replacement

    pieces = [source[: headers[0].start]]
    inserted = False
    for index, header in enumerate(headers):
        block_end = headers[index + 1].start if index + 1 < len(headers) else len(source)
        block = source[header.start:block_end]
        if header.path[: len(target_path)] == target_path:
            if not inserted:
                pieces.append(replacement + newline)
                inserted = True
            lines = block.splitlines(keepends=True)
            trivia_start = len(lines)
            while trivia_start > 1:
                candidate = lines[trivia_start - 1]
                if candidate.strip() and not candidate.lstrip().startswith("#"):
                    break
                trivia_start -= 1
            pieces.append("".join(lines[trivia_start:]))
            continue
        pieces.append(block)
    return "".join(pieces)


def _preflight_toml_content(
    original_content: str | None,
    *,
    logical_path: Path,
    servers_key: str,
    entry: dict,
) -> tuple[bool, str]:
    data = _load_toml(original_content or "", path=logical_path)
    servers = data.setdefault(servers_key, {})
    if not isinstance(servers, dict):
        raise InvalidConfigError(
            f"'{servers_key}' must be a TOML table: {logical_path}"
        )
    existing_entry = servers.get("agent-eyes")
    changed = existing_entry != entry
    if not changed:
        return False, original_content or ""

    rendered = _render_toml_change(
        original_content or "",
        path=logical_path,
        servers_key=servers_key,
        entry=entry,
        existing_entry=existing_entry,
    )
    expected = copy.deepcopy(data)
    expected[servers_key]["agent-eyes"] = entry
    rendered_data = _load_toml(rendered, path=logical_path)
    if not _toml_values_equal(rendered_data, expected):
        raise InvalidConfigError(
            f"Unable to safely preserve unrelated TOML config: {logical_path}"
        )
    return True, rendered


def _toml_values_equal(left: object, right: object) -> bool:
    if isinstance(left, float) and isinstance(right, float):
        if math.isnan(left) and math.isnan(right):
            return True
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            _toml_values_equal(left[key], right[key]) for key in left
        )
    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        return len(left) == len(right) and all(
            _toml_values_equal(left_value, right_value)
            for left_value, right_value in zip(left, right)
        )
    return left == right


def _resolve_executable(executable: str | Path | None) -> Path:
    candidate = Path(executable) if executable is not None else None
    if candidate is None:
        discovered = shutil.which("agent-eyes")
        if discovered is None:
            raise RuntimeError("Persistent agent-eyes executable not found; run agent-eyes setup")
        candidate = Path(discovered)
    candidate = candidate.expanduser()
    if not candidate.is_absolute():
        raise ValueError("Agent Eyes executable must be an absolute path")
    return candidate


def preflight_mcp_file(
    path: Path,
    *,
    servers_key: str,
    executable: str | Path,
    is_zed: bool = False,
    allow_jsonc: bool | None = None,
    config_format: str | None = None,
) -> ConfigurePlan:
    """Validate and render one JSON, JSONC, or TOML change without writing."""
    logical_path = _logical_path(path)
    write_path = _resolve_write_path(logical_path)
    anchor, anchor_device, anchor_inode = _directory_anchor(write_path.parent)
    original_content = _read_text(logical_path)
    resolved = _resolve_executable(executable)
    entry = get_mcp_entry_zed(resolved) if is_zed else get_mcp_entry(resolved)
    selected_format = config_format or (
        "toml" if logical_path.suffix.lower() == ".toml" else "json"
    )
    if selected_format == "toml":
        changed, rendered = _preflight_toml_content(
            original_content,
            logical_path=logical_path,
            servers_key=servers_key,
            entry=entry,
        )
        return ConfigurePlan(
            changed=changed,
            path=str(logical_path),
            write_path=str(write_path),
            original_content=original_content,
            rendered_content=rendered,
            source_format="toml",
            anchor_path=str(anchor),
            anchor_device=anchor_device,
            anchor_inode=anchor_inode,
        )
    if selected_format not in {"json", "jsonc"}:
        raise ValueError(f"Unsupported MCP config format: {selected_format}")

    if original_content is None:
        data: dict = {}
        source_format = "json"
    else:
        jsonc_enabled = (
            is_zed or servers_key in {"servers", "context_servers"}
            if allow_jsonc is None
            else allow_jsonc
        )
        if selected_format == "jsonc":
            jsonc_enabled = True
        data, source_format = _parse_config(
            original_content,
            path=logical_path,
            allow_jsonc=jsonc_enabled,
        )

    servers = data.setdefault(servers_key, {})
    if not isinstance(servers, dict):
        raise InvalidConfigError(
            f"'{servers_key}' must be a JSON object: {logical_path}"
        )
    changed = servers.get("agent-eyes") != entry
    if not changed:
        rendered = original_content or json.dumps(data, indent=2) + "\n"
    elif original_content is not None:
        try:
            rendered = _render_jsonc_change(
                original_content,
                servers_key=servers_key,
                entry=entry,
            )
        except (IndexError, json.JSONDecodeError, ValueError) as exc:
            raise InvalidConfigError(
                f"Unable to safely preserve JSON formatting: {logical_path}"
            ) from exc
        servers["agent-eyes"] = entry
        try:
            rendered_data = (
                _strict_json_loads(rendered)
                if source_format == "json"
                else _load_jsonc(rendered)
            )
        except (json.JSONDecodeError, ValueError) as exc:
            raise InvalidConfigError(
                f"Unable to safely preserve JSON formatting: {logical_path}"
            ) from exc
        if rendered_data != data:
            raise InvalidConfigError(
                f"Unable to safely preserve JSON formatting: {logical_path}"
            )
    else:
        servers["agent-eyes"] = entry
        rendered = json.dumps(data, indent=2) + "\n"

    return ConfigurePlan(
        changed=changed,
        path=str(logical_path),
        write_path=str(write_path),
        original_content=original_content,
        rendered_content=rendered,
        source_format=source_format,
        anchor_path=str(anchor),
        anchor_device=anchor_device,
        anchor_inode=anchor_inode,
    )


def preflight_text_file(
    path: Path,
    *,
    content: str,
    source_format: str = "text",
) -> ConfigurePlan:
    """Preflight one exact text artifact for the shared setup transaction."""
    if not isinstance(content, str):
        raise TypeError("content must be a string")
    if not isinstance(source_format, str) or not source_format:
        raise ValueError("source_format must be a non-empty string")

    logical_path = _logical_path(path)
    write_path = _resolve_write_path(logical_path)
    anchor, anchor_device, anchor_inode = _directory_anchor(write_path.parent)
    original_content = _read_text(logical_path)
    return ConfigurePlan(
        changed=original_content != content,
        path=str(logical_path),
        write_path=str(write_path),
        original_content=original_content,
        rendered_content=content,
        source_format=source_format,
        anchor_path=str(anchor),
        anchor_device=anchor_device,
        anchor_inode=anchor_inode,
    )


def _classify_current_plan(plan: ConfigurePlan) -> str:
    logical_path = Path(plan.path)
    current_write_path = _resolve_write_path(logical_path)
    current_content = _read_text(logical_path)
    if str(current_write_path) != plan.write_path:
        raise InvalidConfigError(
            f"Config changed since preflight; refusing to overwrite: {logical_path}"
        )
    if not plan.changed and current_content == plan.original_content:
        return "unchanged"
    if plan.changed and current_content == plan.rendered_content:
        return "already_applied"
    if current_content != plan.original_content:
        raise InvalidConfigError(
            f"Config changed since preflight; refusing to overwrite: {logical_path}"
        )
    return "pending"


def _rollback_plans(plans: Sequence[ConfigurePlan]) -> list[str]:
    errors: list[str] = []
    for plan in reversed(plans):
        write_path = Path(plan.write_path)
        try:
            if plan.original_content is None:
                if os.name == "nt":
                    _assert_expected_content(write_path, plan.rendered_content)
                    write_path.unlink(missing_ok=True)
                    _fsync_directory(write_path.parent)
                else:
                    _unlink_expected_posix(
                        write_path,
                        plan.rendered_content,
                        anchor_path=plan.anchor_path,
                        anchor_device=plan.anchor_device,
                        anchor_inode=plan.anchor_inode,
                    )
            else:
                _atomic_write(
                    write_path,
                    plan.original_content,
                    expected_content=plan.rendered_content,
                    anchor_path=plan.anchor_path,
                    anchor_device=plan.anchor_device,
                    anchor_inode=plan.anchor_inode,
                )
        except (InvalidConfigError, OSError) as exc:
            errors.append(f"{plan.path}: {type(exc).__name__}")
    return errors


def apply_mcp_plans(
    plans: Sequence[ConfigurePlan],
    *,
    backups_dir: Path | None = None,
    lock_path: Path | None = None,
) -> tuple[ConfigureResult, ...]:
    """Atomically apply a fully preflighted group under the setup process lock."""
    prepared = tuple(plans)
    write_paths = [plan.write_path for plan in prepared]
    if len(write_paths) != len(set(write_paths)):
        raise InvalidConfigError("A setup transaction contains duplicate config targets")
    if not prepared:
        return ()

    with setup_process_lock(lock_path):
        states = tuple(_classify_current_plan(plan) for plan in prepared)
        pending = [
            plan
            for plan, state in zip(prepared, states)
            if state == "pending"
        ]
        backups = {
            plan.path: _backup(Path(plan.write_path), backups_dir)
            for plan in pending
        }

        # Backups are copied from the live targets. Revalidate the entire group
        # after those reads and before the first config write.
        refreshed = tuple(_classify_current_plan(plan) for plan in prepared)
        for previous, current in zip(states, refreshed):
            if previous != current:
                raise InvalidConfigError(
                    "A config changed while setup was preparing backups; no config was written"
                )

        written: list[ConfigurePlan] = []
        try:
            for plan, state in zip(prepared, states):
                if state != "pending":
                    continue
                _atomic_write(
                    Path(plan.write_path),
                    plan.rendered_content,
                    expected_content=plan.original_content,
                    anchor_path=plan.anchor_path,
                    anchor_device=plan.anchor_device,
                    anchor_inode=plan.anchor_inode,
                )
                written.append(plan)
            for plan in written:
                current_path = _resolve_write_path(Path(plan.path))
                if str(current_path) != plan.write_path:
                    raise InvalidConfigError(
                        f"Config target could not be verified after write: {plan.path}"
                    )
        except Exception as exc:
            rollback_errors = _rollback_plans(written)
            if rollback_errors:
                raise InvalidConfigError(
                    "Setup write failed and rollback was incomplete: "
                    + ", ".join(rollback_errors)
                ) from exc
            raise

        return tuple(
            ConfigureResult(
                changed=state == "pending",
                applied=state == "pending",
                path=plan.path,
                backup=backups.get(plan.path),
            )
            for plan, state in zip(prepared, states)
        )


def apply_mcp_plan(
    plan: ConfigurePlan,
    *,
    backups_dir: Path | None = None,
    lock_path: Path | None = None,
) -> ConfigureResult:
    """Apply one plan through the same locked, idempotent transaction path."""
    return apply_mcp_plans(
        (plan,),
        backups_dir=backups_dir,
        lock_path=lock_path,
    )[0]


def configure_mcp_file(
    path: Path,
    *,
    servers_key: str,
    executable: str | Path,
    is_zed: bool = False,
    allow_jsonc: bool | None = None,
    config_format: str | None = None,
    dry_run: bool = False,
    backups_dir: Path | None = None,
    lock_path: Path | None = None,
) -> ConfigureResult:
    """Safely add or update Agent Eyes in one JSON or supported JSONC config."""
    plan = preflight_mcp_file(
        path,
        servers_key=servers_key,
        executable=executable,
        is_zed=is_zed,
        allow_jsonc=allow_jsonc,
        config_format=config_format,
    )
    if dry_run:
        return ConfigureResult(changed=plan.changed, applied=False, path=plan.path)
    return apply_mcp_plan(
        plan,
        backups_dir=backups_dir,
        lock_path=lock_path,
    )


# ── MCP Config Modification ─────────────────────────────────────────

def _add_agent_eyes_to_mcp(
    config_path: Path,
    servers_key: str,
    is_zed: bool = False,
    executable: str | Path | None = None,
) -> dict:
    """Add agent-eyes MCP entry to a config file.

    Returns action report.
    """
    result = configure_mcp_file(
        config_path,
        servers_key=servers_key,
        executable=_resolve_executable(executable),
        is_zed=is_zed,
    )

    return {
        "action": "added_mcp_entry" if result.changed else "mcp_entry_unchanged",
        "path": str(config_path),
        "backup": result.backup,
        "detail": (
            "Added agent-eyes MCP server entry"
            if result.changed
            else "Agent Eyes MCP server entry already current"
        ),
    }


def _remove_competitor_from_mcp(
    config_path: Path,
    servers_key: str,
    server_key_to_remove: str,
) -> dict | None:
    """Remove a specific competitor server entry from MCP config.

    Returns action report or None if nothing to do.
    """
    data = _read_json(config_path)
    if not data:
        return None

    servers = data.get(servers_key, {})
    if server_key_to_remove not in servers:
        return None

    backup = _backup(config_path)
    del data[servers_key][server_key_to_remove]
    _write_json(config_path, data)

    return {
        "action": "removed_competitor",
        "path": str(config_path),
        "backup": backup,
        "detail": f"Removed '{server_key_to_remove}' from {servers_key}",
    }


# ── Claude Code Skill Installation ──────────────────────────────────

def _install_skill(level: str) -> dict:
    """Install agent-eyes SKILL.md at the specified level.

    Levels:
      - "global": ~/.claude/skills/agent-eyes/SKILL.md
      - "project": .claude/skills/agent-eyes/SKILL.md (cwd)
    """
    if level == "global":
        skill_dir = Path.home() / ".claude" / "skills" / "agent-eyes"
    else:
        skill_dir = Path.cwd() / ".claude" / "skills" / "agent-eyes"

    skill_file = skill_dir / "SKILL.md"
    backup = _backup(skill_file)

    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_file.write_text(SKILL_MD)

    return {
        "action": "installed_skill",
        "path": str(skill_file),
        "backup": backup,
        "detail": f"Installed agent-eyes SKILL.md ({level} level)",
    }


# ── Claude Code CLAUDE.md Update ────────────────────────────────────

def _update_claude_md(level: str) -> dict:
    """Add agent-eyes priority section to CLAUDE.md.

    Only adds if not already present. Preserves existing content.

    Levels:
      - "global": ~/.claude/CLAUDE.md
      - "project": ./CLAUDE.md (cwd)
    """
    if level == "global":
        claude_md = Path.home() / ".claude" / "CLAUDE.md"
    else:
        claude_md = Path.cwd() / "CLAUDE.md"

    existing = ""
    if claude_md.exists():
        existing = claude_md.read_text()

    # Check if already has agent-eyes section
    if "agent-eyes Priority" in existing:
        return {
            "action": "skipped_claude_md",
            "path": str(claude_md),
            "backup": None,
            "detail": "CLAUDE.md already has agent-eyes section",
        }

    backup = _backup(claude_md)

    # Append section
    new_content = existing.rstrip() + "\n\n" + CLAUDE_MD_SECTION + "\n"
    claude_md.parent.mkdir(parents=True, exist_ok=True)
    claude_md.write_text(new_content)

    return {
        "action": "updated_claude_md",
        "path": str(claude_md),
        "backup": backup,
        "detail": f"Added agent-eyes priority section to CLAUDE.md ({level} level)",
    }


# ── Claude Code Agent Definition Update ─────────────────────────────

def _update_agent_definitions(
    agents_dir: Path,
    competitor_ids: list[str],
) -> list[dict]:
    """Replace competitor tool references in agent .md files.

    For each agent file that references a competitor's tools, replace those
    tool references with agent-eyes equivalents.

    CAREFUL: Only replaces the specific competitor tool patterns, preserves
    everything else in the file.
    """
    results = []
    if not agents_dir.exists():
        return results

    ae_tools = get_agent_eyes_tools_list()

    for agent_file in agents_dir.glob("*.md"):
        try:
            content = agent_file.read_text()
        except OSError:
            continue

        for comp_id in competitor_ids:
            pattern = COMPETITOR_TOOL_PATTERNS.get(comp_id)
            if not pattern:
                continue

            # Find all competitor tool references (e.g., mcp__playwright__browser_click)
            matches = re.findall(pattern, content)
            if not matches:
                continue

            # Replace the entire block of competitor tools with agent-eyes tools
            # Strategy: find the contiguous sequence of competitor tools and replace
            # with the agent-eyes tools list.
            #
            # Competitor tools appear as comma-separated in the `tools:` frontmatter.
            # We replace each individual tool ref, then deduplicate the agent-eyes
            # tools that got inserted multiple times.

            backup = _backup(agent_file)

            # Replace all individual tool refs with a placeholder
            placeholder = "<<AGENT_EYES_TOOLS>>"
            new_content = content
            for match in matches:
                new_content = new_content.replace(match, placeholder)

            # Now collapse multiple adjacent placeholders (with commas/spaces between)
            # into a single agent-eyes tools list
            collapse_pattern = r"(?:<<AGENT_EYES_TOOLS>>(?:\s*,\s*)?)+<<AGENT_EYES_TOOLS>>"
            while re.search(collapse_pattern, new_content):
                new_content = re.sub(collapse_pattern, placeholder, new_content)

            # Replace the remaining single placeholder with actual tools
            new_content = new_content.replace(placeholder, ae_tools)

            # Clean up any resulting double commas or trailing commas
            new_content = re.sub(r",\s*,", ",", new_content)
            new_content = re.sub(r",\s*$", "", new_content, flags=re.MULTILINE)

            agent_file.write_text(new_content)
            results.append({
                "action": "updated_agent",
                "path": str(agent_file),
                "backup": backup,
                "detail": (
                    f"Replaced {len(matches)} {comp_id} tool refs "
                    f"with agent-eyes tools in {agent_file.name}"
                ),
            })

    return results


# ── Main Apply Function ─────────────────────────────────────────────

def apply_setup(
    replace_competitors: list[str],
    configure_tools: list[str],
    level: str = "global",
    scan_report: dict | None = None,
    *,
    executable: str | Path | None = None,
    backups_dir: Path | None = None,
    lock_path: Path | None = None,
    dry_run: bool = False,
    consent: bool = False,
) -> dict:
    """Plan and apply exact Agent Eyes entries for all selected clients.

    Competitor removal and broad instruction mutation are intentionally not
    part of normal setup. The legacy ``replace_competitors`` argument remains
    accepted for client compatibility, but is reported and ignored.
    """
    if level not in {"global", "project"}:
        raise ValueError("Setup level must be 'global' or 'project'")
    if not configure_tools:
        raise ValueError("At least one AI tool must be selected")

    warnings: list[str] = []
    if replace_competitors:
        warnings.append(
            "Detected competitor MCPs and skills were not removed or disabled; "
            "normal setup coexists with unrelated tools."
        )
    del scan_report

    tool_defs = {d["id"]: d for d in _ai_tool_definitions()}
    selected_ids = tuple(dict.fromkeys(configure_tools))
    unknown = [tool_id for tool_id in selected_ids if tool_id not in tool_defs]
    if unknown:
        raise ValueError(f"Unknown AI tool(s): {', '.join(unknown)}")

    resolved_executable = _resolve_executable(executable)
    plan_items: list[tuple[str, ConfigurePlan]] = []
    location_key = "global_mcp" if level == "global" else "project_mcp"
    project_root = Path.cwd().resolve(strict=True) if level == "project" else None
    for tool_id in selected_ids:
        definition = tool_defs[tool_id]
        location = definition.get("config_locations", {}).get(location_key)
        if location is None:
            warnings.append(
                f"{tool_id} does not declare a {level} MCP configuration target"
            )
            continue
        path = Path(location["path"])
        if not path.is_absolute():
            path = Path.cwd() / path
        config_format = str(location.get("format", "json"))
        plan = preflight_mcp_file(
            path,
            servers_key=str(location["key"]),
            executable=resolved_executable,
            is_zed=tool_id == "zed",
            config_format=config_format,
        )
        if project_root is not None:
            try:
                Path(plan.write_path).relative_to(project_root)
            except ValueError as exc:
                raise InvalidConfigError(
                    f"Project config target resolves outside the project: {plan.path}"
                ) from exc
        plan_items.append((tool_id, plan))

    preview = [
        {
            "tool": tool_id,
            "action": "planned_mcp_entry" if plan.changed else "mcp_entry_unchanged",
            "path": plan.path,
            "target_path": plan.write_path if plan.write_path != plan.path else None,
            "backup": None,
            "changed": plan.changed,
            "applied": False,
            "detail": (
                "Will add or update only the Agent Eyes MCP entry"
                if plan.changed
                else "Agent Eyes MCP entry is already current"
            ),
        }
        for tool_id, plan in plan_items
    ]
    backups_path = backups_dir or get_backups_path()
    if dry_run or not consent:
        return {
            "changes": preview,
            "warnings": warnings,
            "backups_dir": str(backups_path),
            "applied": False,
            "cancelled": not consent,
            "dry_run": dry_run,
        }

    results = apply_mcp_plans(
        tuple(plan for _, plan in plan_items),
        backups_dir=backups_dir,
        lock_path=lock_path,
    )
    changes = []
    for (tool_id, plan), result in zip(plan_items, results):
        changes.append(
            {
                "tool": tool_id,
                "action": "added_mcp_entry" if result.applied else "mcp_entry_unchanged",
                "path": plan.path,
                "target_path": plan.write_path if plan.write_path != plan.path else None,
                "backup": result.backup,
                "changed": result.changed,
                "applied": result.applied,
                "detail": (
                    "Added or updated only the Agent Eyes MCP entry"
                    if result.applied
                    else "Agent Eyes MCP entry is already current"
                ),
            }
        )

    return {
        "changes": changes,
        "warnings": warnings,
        "backups_dir": str(backups_path),
        "applied": any(result.applied for result in results),
        "cancelled": False,
        "dry_run": False,
    }
