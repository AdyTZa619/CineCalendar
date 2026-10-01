from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import zipfile

import pytest

from cinecalendar.updater import (
    POST_UPDATE_MODE,
    PREVIEW_MANIFEST_URL,
    _powershell_helper,
    _safe_extract_zip,
    _repair_frozen_ca_bundle,
    check_for_update,
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


def test_alpha_manifest_is_accepted_only_on_alpha_channel():
    info = parse_manifest(
        valid_manifest(
            version="5.0 Alpha 2",
            url="https://example.invalid/CineCalendar-V5-Alpha-Windows-x64.zip",
            channel="alpha",
        ),
        expected_channel="alpha",
    )
    assert info.version == "5.0 Alpha 2"
    assert info.channel == "alpha"
    with pytest.raises(ValueError):
        parse_manifest(
            valid_manifest(
                version="5.0 Alpha 2",
                url="https://example.invalid/CineCalendar-V5-Alpha-Windows-x64.zip",
                channel="alpha",
            )
        )


def test_preview_updater_uses_separate_manifest(monkeypatch):
    import cinecalendar.updater as updater
    requested = []

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return valid_manifest(version="4.16.2", channel="preview")

    def get(url, **_kwargs):
        requested.append(url)
        return Response()

    monkeypatch.setattr(updater.requests, "get", get)
    info = check_for_update("4.16.1")
    assert info is not None and info.version == "4.16.2"
    assert info.channel == "preview"
    assert requested == [PREVIEW_MANIFEST_URL]
    assert check_for_update("4.16.2") is None


@pytest.mark.skipif(os.name != "nt", reason="PowerShell update integration requires Windows")
def test_updater_rollback_preserves_nested_alpha_database(tmp_path):
    executable = shutil.which("where.exe")
    if not executable:
        pytest.skip("Windows where.exe unavailable")
    app = tmp_path / "CineCalendar"
    data = app / "CineCalendarData"
    updates = data / "updates"
    alpha = app / "V5Alpha" / "CineCalendarV5AlphaData" / "data" / "cinecalendar.db"
    alpha.parent.mkdir(parents=True)
    alpha.write_bytes(b"original Alpha database marker")
    current = app / "CineCalendar.exe"
    shutil.copy2(executable, current)
    staged = updates / "staged"
    staged.mkdir(parents=True)
    (staged / "CineCalendar.exe").write_bytes(b"MZinvalid test executable")
    updates.mkdir(parents=True, exist_ok=True)
    helper_path = updates / "apply_update.ps1"
    helper_path.write_text(_powershell_helper(), encoding="utf-8-sig")
    request_path = updates / "request.json"
    request_path.write_text(json.dumps({
        "parent_pid": 2147483000, "app_root": str(app), "data_root": str(data),
        "staged_dir": str(staged), "backup_dir": str(updates / "backup" / "previous"),
        "pending_zip": str(updates / "pending.zip"), "health": str(updates / "health.ok"),
        "log": str(updates / "updater.log"), "expected_version": "4.16.2",
        "exe_name": "CineCalendar.exe",
    }), encoding="utf-8")
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(helper_path), str(request_path)],
        capture_output=True, text=True, timeout=25,
    )
    assert result.returncode == 4, (result.stdout, result.stderr, (updates / "updater.log").read_text())
    assert alpha.read_bytes() == b"original Alpha database marker"


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


def test_safe_extract_accepts_alpha_onedir_bundle(tmp_path):
    archive = tmp_path / "alpha.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("CineCalendar-V5-Alpha.exe", b"MZ" + b"alpha executable")
        z.writestr("_internal/runtime.dll", b"runtime")
    out = tmp_path / "stage-alpha"
    _safe_extract_zip(archive, out, exe_name="CineCalendar-V5-Alpha.exe")
    assert (out / "CineCalendar-V5-Alpha.exe").read_bytes().startswith(b"MZ")
    assert (out / "_internal" / "runtime.dll").is_file()


def test_safe_extract_rejects_zip_slip(tmp_path):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("../escape.txt", b"bad")
        z.writestr("CineCalendar.exe", b"MZxx")
        z.writestr("_internal/runtime.dll", b"runtime")
    with pytest.raises(RuntimeError):
        _safe_extract_zip(archive, tmp_path / "stage")


def test_frozen_updater_removes_stale_ca_environment(monkeypatch, tmp_path):
    import cinecalendar.updater as updater
    missing = tmp_path / "old-build" / "_internal" / "certifi" / "cacert.pem"
    monkeypatch.setattr(updater.sys, "frozen", True, raising=False)
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(missing))
    monkeypatch.setenv("CURL_CA_BUNDLE", str(missing))
    monkeypatch.setenv("SSL_CERT_FILE", str(missing))
    _repair_frozen_ca_bundle()
    assert "REQUESTS_CA_BUNDLE" not in updater.os.environ
    assert "CURL_CA_BUNDLE" not in updater.os.environ
    assert "SSL_CERT_FILE" not in updater.os.environ


def test_frozen_updater_keeps_valid_ca_environment(monkeypatch, tmp_path):
    import cinecalendar.updater as updater
    ca = tmp_path / "cacert.pem"
    ca.write_text("test", encoding="utf-8")
    monkeypatch.setattr(updater.sys, "frozen", True, raising=False)
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(ca))
    _repair_frozen_ca_bundle()
    assert updater.os.environ["REQUESTS_CA_BUNDLE"] == str(ca)
