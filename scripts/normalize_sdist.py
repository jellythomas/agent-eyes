#!/usr/bin/env python3
"""Rewrite a PEP 517 ``.tar.gz`` source distribution deterministically."""

from __future__ import annotations

import argparse
import copy
import gzip
import os
from pathlib import Path, PurePosixPath
import shutil
import tarfile
import tempfile


def _source_date_epoch() -> int:
    raw = os.environ.get("SOURCE_DATE_EPOCH")
    if raw is None:
        raise SystemExit("SOURCE_DATE_EPOCH must be set")
    try:
        epoch = int(raw)
    except ValueError as exc:
        raise SystemExit("SOURCE_DATE_EPOCH must be an integer") from exc
    if not 0 <= epoch <= 0xFFFFFFFF:
        raise SystemExit("SOURCE_DATE_EPOCH must fit the gzip timestamp field")
    return epoch


def _validated_members(
    archive: tarfile.TarFile,
    expected_root: str,
) -> list[tarfile.TarInfo]:
    members = archive.getmembers()
    if not members:
        raise ValueError("sdist is empty")

    roots: set[str] = set()
    by_name: dict[str, tarfile.TarInfo] = {}
    for member in members:
        raw_parts = member.name.split("/")
        path = PurePosixPath(member.name)
        if (
            path.is_absolute()
            or any(part in {"", ".", ".."} for part in raw_parts)
            or "\\" in member.name
            or "\0" in member.name
            or not path.parts
        ):
            raise ValueError(f"unsafe archive member: {member.name!r}")
        if member.name in by_name:
            raise ValueError(f"duplicate archive member: {member.name!r}")
        if not (member.isfile() or member.isdir()):
            raise ValueError(f"unsupported archive member type: {member.name!r}")
        by_name[member.name] = member
        roots.add(path.parts[0])

    if roots != {expected_root}:
        raise ValueError(
            f"sdist must contain only the top-level directory {expected_root!r}"
        )
    root = by_name.get(expected_root)
    if root is None or not root.isdir():
        raise ValueError(f"sdist root must be a directory: {expected_root!r}")
    required = (
        f"{expected_root}/PKG-INFO",
        f"{expected_root}/pyproject.toml",
    )
    missing = [name for name in required if name not in by_name]
    if missing:
        raise ValueError(f"sdist is missing required members: {missing!r}")
    invalid = [name for name in required if not by_name[name].isfile()]
    if invalid:
        raise ValueError(f"sdist required members must be files: {invalid!r}")
    return sorted(members, key=lambda member: member.name)


def normalize_sdist(path: Path, epoch: int) -> None:
    """Validate and atomically normalize one source distribution in place."""
    if not 0 <= epoch <= 0xFFFFFFFF:
        raise ValueError("epoch must fit the gzip timestamp field")
    if path.is_symlink():
        raise ValueError(f"sdist must not be a symlink: {path}")
    path = path.resolve(strict=True)
    if not path.is_file() or not path.name.endswith(".tar.gz"):
        raise ValueError(f"expected a .tar.gz sdist: {path}")

    expected_root = path.name.removesuffix(".tar.gz")
    temporary_path: Path | None = None
    try:
        with tarfile.open(path, mode="r:gz") as source:
            members = _validated_members(source, expected_root)
            with tempfile.NamedTemporaryFile(
                mode="w+b",
                prefix=f".{path.name}.",
                suffix=".tmp",
                dir=path.parent,
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                with gzip.GzipFile(
                    filename="",
                    mode="wb",
                    fileobj=temporary,
                    compresslevel=9,
                    mtime=epoch,
                ) as compressed:
                    with tarfile.open(
                        fileobj=compressed,
                        mode="w",
                        format=tarfile.PAX_FORMAT,
                    ) as target:
                        for member in members:
                            normalized = copy.copy(member)
                            normalized.mtime = epoch
                            normalized.uid = 0
                            normalized.gid = 0
                            normalized.uname = ""
                            normalized.gname = ""
                            normalized.mode = 0o644 if member.isfile() else 0o755
                            normalized.linkname = ""
                            normalized.devmajor = 0
                            normalized.devminor = 0
                            normalized.pax_headers = {}
                            if member.isfile():
                                payload = source.extractfile(member)
                                if payload is None:
                                    raise ValueError(
                                        "missing payload for archive member: "
                                        f"{member.name!r}"
                                    )
                                with payload:
                                    target.addfile(normalized, payload)
                            else:
                                target.addfile(normalized)
                temporary.flush()
                os.fsync(temporary.fileno())
        if temporary_path is None:
            raise RuntimeError("normalization did not produce an archive")
        shutil.copymode(path, temporary_path)
        os.replace(temporary_path, path)
        temporary_path = None
        try:
            directory = os.open(path.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(directory)
        except OSError:
            pass
        finally:
            os.close(directory)
    except BaseException:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sdist", nargs="+", type=Path)
    arguments = parser.parse_args()
    epoch = _source_date_epoch()
    for path in arguments.sdist:
        normalize_sdist(path, epoch)


if __name__ == "__main__":
    main()
