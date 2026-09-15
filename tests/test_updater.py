from __future__ import annotations

import hashlib
import zipfile

import pytest

from cinecalendar.updater import (
    POST_UPDATE_MODE,
    _safe_extract_zip,
    health_matches,
    is_newer_version,
    parse_manifest,
    parse_special_startup,
    sha256_path,
    write_health_marker,
)


def valid_manifest(**overrides):
    data = {
        "version": "2.2.1",
        "url": "https://example.invalid/CineCalendar-2.2.1-Premium-Windows-x64.zip",
        "sha256": "a" * 64,
        "notes": "test",
        "publishedAt": "2026-09-14T00:00:00Z",
        "channel": "stable",
    }
    data.update(overrides)
    return data


def test_version_compare_is_numeric_not_lexicographic():
    assert is_newer_version("2.2.1", "2.2.0")
    assert is_newer_version("2.10.0", "2.9.9")
    assert is_newer_version("3.0", "2.99.99")
    assert not is_newer_version("2.2.1", "2.2.1")
    assert not is_newer_version("2.1.9", "2.2.0")


def test_manifest_requires_https_hash_and_stable_channel():
    info = parse_manifest(valid_manifest())
    assert info.version == "2.2.1"
    assert info.channel == "stable"
    assert info.sha256 == "a" * 64

    with pytest.raises(ValueError):
        parse_manifest(valid_manifest(url="http://example.invalid/file.zip"))
    with pytest.raises(ValueError):
        parse_manifest(valid_manifest(sha256="1234"))
    with pytest.raises(ValueError):
        parse_manifest(valid_manifest(channel="test"))


def test_sha256_path_matches_known_digest(tmp_path):
    p = tmp_path / "payload.zip"
    payload = b"CineCalendar updater payload\x00\x01"
    p.write_bytes(payload)
    assert sha256_path(p) == hashlib.sha256(payload).hexdigest()


def test_health_marker_is_version_bound(tmp_path):
    p = tmp_path / "health.ok"
    write_health_marker(p, "2.2.1")
    assert health_matches(p, "2.2.1")
    assert not health_matches(p, "2.2.0")


def test_post_update_mode_does_not_enter_helper(tmp_path):
    health = tmp_path / "health.ok"
    exit_code, post = parse_special_startup([
        "CineCalendar.exe", POST_UPDATE_MODE, str(health), "2.2.1"
    ])
    assert exit_code is None
    assert post == (str(health), "2.2.1")


def test_safe_extract_accepts_premium_onedir_bundle(tmp_path):
    archive = tmp_path / "premium.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("CineCalendar.exe", b"MZ" + b"test executable")
        z.writestr("_internal/runtime.dll", b"runtime")
        z.writestr("README.txt", b"premium")
    out = tmp_path / "stage"
    _safe_extract_zip(archive, out)
    assert (out / "CineCalendar.exe").read_bytes().startswith(b"MZ")
    assert (out / "_internal" / "runtime.dll").is_file()


def test_safe_extract_rejects_zip_slip(tmp_path):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("../escape.txt", b"bad")
        z.writestr("CineCalendar.exe", b"MZxx")
        z.writestr("_internal/runtime.dll", b"runtime")
    with pytest.raises(RuntimeError):
        _safe_extract_zip(archive, tmp_path / "stage")
