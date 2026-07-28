from __future__ import annotations

import gzip
import importlib.util
import io
from pathlib import Path
import tarfile

import pytest


ROOT = "agent_eyes-0.10.0"
EPOCH = 1_784_692_386
SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "normalize_sdist.py"
SPEC = importlib.util.spec_from_file_location("normalize_sdist", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
normalize_sdist = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(normalize_sdist)


def _member(
    name: str,
    *,
    timestamp: int,
    payload: bytes | None = None,
    type_: bytes | None = None,
) -> tuple[tarfile.TarInfo, bytes | None]:
    info = tarfile.TarInfo(name)
    info.type = type_ or (tarfile.DIRTYPE if payload is None else tarfile.REGTYPE)
    info.size = len(payload) if payload is not None else 0
    info.mode = 0o777 if payload is None else 0o666
    info.mtime = timestamp
    info.uid = 501
    info.gid = 20
    info.uname = "builder"
    info.gname = "staff"
    info.pax_headers = {"mtime": f"{timestamp}.25"}
    return info, payload


def _write_archive(
    path: Path,
    *,
    timestamp: int,
    reverse: bool = False,
    extra: list[tuple[tarfile.TarInfo, bytes | None]] | None = None,
    include_required: bool = True,
) -> None:
    members = [_member(ROOT, timestamp=timestamp)]
    if include_required:
        members.extend(
            [
                _member(
                    f"{ROOT}/PKG-INFO",
                    timestamp=timestamp,
                    payload=b"Name: agent-eyes\n",
                ),
                _member(
                    f"{ROOT}/pyproject.toml",
                    timestamp=timestamp,
                    payload=b"[build-system]\n",
                ),
            ]
        )
    members.extend(
        [
            _member(f"{ROOT}/src", timestamp=timestamp),
            _member(
                f"{ROOT}/src/module.py",
                timestamp=timestamp,
                payload=b"answer = 42\n",
            ),
        ]
    )
    members.extend(extra or [])
    if reverse:
        members.reverse()
    with path.open("wb") as raw:
        with gzip.GzipFile(
            filename=path.name,
            mode="wb",
            fileobj=raw,
            mtime=timestamp,
        ) as compressed:
            with tarfile.open(
                fileobj=compressed,
                mode="w",
                format=tarfile.PAX_FORMAT,
            ) as archive:
                for info, payload in members:
                    archive.addfile(
                        info,
                        io.BytesIO(payload) if payload is not None else None,
                    )


def test_normalize_sdist_is_reproducible_idempotent_and_preserves_payload(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first" / f"{ROOT}.tar.gz"
    second = tmp_path / "second" / f"{ROOT}.tar.gz"
    first.parent.mkdir()
    second.parent.mkdir()
    _write_archive(first, timestamp=EPOCH + 10)
    _write_archive(second, timestamp=EPOCH + 20, reverse=True)

    normalize_sdist.normalize_sdist(first, EPOCH)
    normalize_sdist.normalize_sdist(second, EPOCH)

    assert first.read_bytes() == second.read_bytes()
    normalized_bytes = first.read_bytes()
    normalize_sdist.normalize_sdist(first, EPOCH)
    assert first.read_bytes() == normalized_bytes
    assert int.from_bytes(normalized_bytes[4:8], "little") == EPOCH
    assert normalized_bytes[9] == 255
    with tarfile.open(first, mode="r:gz") as archive:
        members = archive.getmembers()
        assert [item.name for item in members] == sorted(item.name for item in members)
        assert {
            (
                item.mtime,
                item.uid,
                item.gid,
                item.uname,
                item.gname,
                item.linkname,
                item.devmajor,
                item.devminor,
                tuple(item.pax_headers.items()),
            )
            for item in members
        } == {(EPOCH, 0, 0, "", "", "", 0, 0, ())}
        assert {item.mode for item in members if item.isfile()} == {0o644}
        assert {item.mode for item in members if item.isdir()} == {0o755}
        payload = archive.extractfile(f"{ROOT}/src/module.py")
        assert payload is not None
        assert payload.read() == b"answer = 42\n"


@pytest.mark.parametrize(
    "extra",
    [
        [_member("/absolute", timestamp=EPOCH, payload=b"unsafe")],
        [_member(f"{ROOT}/../escape", timestamp=EPOCH, payload=b"unsafe")],
        [_member(f"{ROOT}/./hidden", timestamp=EPOCH, payload=b"unsafe")],
        [_member(f"{ROOT}\\escape", timestamp=EPOCH, payload=b"unsafe")],
        [_member(f"{ROOT}/PKG-INFO", timestamp=EPOCH, payload=b"duplicate")],
        [_member(f"{ROOT}/link", timestamp=EPOCH, type_=tarfile.SYMTYPE)],
        [_member("another-root/file", timestamp=EPOCH, payload=b"second root")],
    ],
)
def test_invalid_archive_is_rejected_without_overwriting(
    tmp_path: Path,
    extra: list[tuple[tarfile.TarInfo, bytes | None]],
) -> None:
    path = tmp_path / f"{ROOT}.tar.gz"
    _write_archive(path, timestamp=EPOCH + 10, extra=extra)
    original = path.read_bytes()

    with pytest.raises(ValueError):
        normalize_sdist.normalize_sdist(path, EPOCH)

    assert path.read_bytes() == original
    assert list(tmp_path.glob(f".{path.name}.*.tmp")) == []


def test_missing_required_member_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / f"{ROOT}.tar.gz"
    _write_archive(path, timestamp=EPOCH, include_required=False)

    with pytest.raises(ValueError, match="missing required members"):
        normalize_sdist.normalize_sdist(path, EPOCH)


def test_source_date_epoch_requires_a_valid_gzip_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SOURCE_DATE_EPOCH", raising=False)
    with pytest.raises(SystemExit, match="must be set"):
        normalize_sdist._source_date_epoch()
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "not-an-int")
    with pytest.raises(SystemExit, match="must be an integer"):
        normalize_sdist._source_date_epoch()
    monkeypatch.setenv("SOURCE_DATE_EPOCH", str(0x1_0000_0000))
    with pytest.raises(SystemExit, match="must fit"):
        normalize_sdist._source_date_epoch()
    monkeypatch.setenv("SOURCE_DATE_EPOCH", str(EPOCH))
    assert normalize_sdist._source_date_epoch() == EPOCH
