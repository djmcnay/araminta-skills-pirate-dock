"""Autopromote gate fixes (RESEARCH.md fixes 1-5): scan-limit classification,
unified clamscan flags, identity-verified skip, in-flight reset suppression,
stale-state reap. Stdlib only; ClamAV/docker/network all mocked."""

import importlib.util
import os
import subprocess
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "dock-autopromote.py"
spec = importlib.util.spec_from_file_location("dock_autopromote_gates", SCRIPT)
assert spec and spec.loader
ap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ap)


@pytest.fixture
def mkv(tmp_path):
    path = tmp_path / "film.mkv"
    path.write_bytes(b"\x1aE\xdf\xa3" + b"\x00" * 128)
    return path


@pytest.fixture
def fake_clamscan(tmp_path, monkeypatch):
    """Fake clamscan honouring the REAL last-arg path; behaviour via env."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    scanner = bindir / "clamscan"
    scanner.write_text(
        "#!/bin/sh\n"
        "last=\"\"; for a in \"$@\"; do last=\"$a\"; done\n"
        "case \"${FAKE_CLAMSCAN_RESULT:-clean}\" in\n"
        "  clean) echo \"$last: OK\"; exit 0 ;;\n"
        "  limits) echo \"$last: Heuristics.Limits.Exceeded.MaxScanTime FOUND\"; exit 1 ;;\n"
        "  infected) echo \"$last: Win.Test.EICAR_HDB-1 FOUND\"; exit 1 ;;\n"
        "  error) echo \"LibClamAV Error: scanner database error\" >&2; exit 2 ;;\n"
        "esac\n"
    )
    scanner.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    return monkeypatch


@pytest.fixture
def quiet(monkeypatch):
    logs: list = []
    monkeypatch.setattr(ap, "log", logs.append)
    monkeypatch.setattr(ap, "save_state", lambda _state: None)
    return logs


def test_limits_heuristic_is_scan_limit_not_malware(mkv, fake_clamscan):
    fake_clamscan.setenv("FAKE_CLAMSCAN_RESULT", "limits")

    status, detail = ap.validate_media(mkv)

    assert status == "scan-limit"
    assert status != "malware-detected"
    assert "Heuristics.Limits.Exceeded" in detail


def test_force_still_resets_scan_limit():
    assert "scan-limit" in ap.FORCE_RESET_STAGES


def test_real_signature_stays_malware_detected(mkv, fake_clamscan):
    fake_clamscan.setenv("FAKE_CLAMSCAN_RESULT", "infected")

    status, detail = ap.validate_media(mkv)

    assert status == "malware-detected"
    assert "FOUND" in detail


def test_scanner_error_rc2_is_scan_failed(mkv, fake_clamscan):
    fake_clamscan.setenv("FAKE_CLAMSCAN_RESULT", "error")

    status, detail = ap.validate_media(mkv)

    assert status == "scan-failed"
    assert "scanner error" in detail.lower()


def test_scanner_timeout_is_scan_failed(mkv, monkeypatch):
    def boom(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd="clamscan", timeout=900)

    monkeypatch.setattr(subprocess, "run", boom)

    status, detail = ap.validate_media(mkv)

    assert status == "scan-failed"
    assert "timed out" in detail.lower()


def test_scan_limit_is_retryable_not_terminal(tmp_path, quiet):
    """stage=scan-limit must NOT early-return: the next pass re-scans."""
    item = tmp_path / "Some.Film.2026.1080p.WEBRip"
    item.mkdir()
    (item / "film.mkv").write_bytes(b"\x1aE\xdf\xa3" + b"\x00" * 64)
    sig = ap.source_signature(ap.video_files_of(item))
    state = {item.name: {"stage": "scan-limit", "source_sig": sig}}
    calls: list = []

    def fake_validate(_path):
        calls.append(_path)
        return (ap.SCAN_LIMIT_STAGE, "Heuristics.Limits.Exceeded.MaxScanTime FOUND")

    import unittest.mock as mock

    with mock.patch.object(ap, "validate_media", side_effect=fake_validate):
        ap.promote_item(item, state, dry=False)

    assert len(calls) == 1
    assert state[item.name]["stage"] == "scan-limit"
    assert any("SCAN-LIMIT" in line for line in quiet)


def test_in_flight_does_not_reset_terminal_state(tmp_path, quiet):
    """Control-file present -> return BEFORE the source-change reset."""
    item = tmp_path / "Some.Film.2026.1080p.WEBRip"
    item.mkdir()
    (item / "film.mkv").write_bytes(b"\x1aE\xdf\xa3" + b"\x00" * 64)
    (item / "film.mkv.aria2").write_bytes(b"control")
    state = {item.name: {"stage": "decode-failed", "source_sig": "stale-old-sig"}}

    import unittest.mock as mock

    with mock.patch.object(
        ap, "validate_media",
        side_effect=AssertionError("must not scan an in-flight item"),
    ):
        ap.promote_item(item, state, dry=False)

    assert state[item.name]["stage"] == "decode-failed"
    assert state[item.name]["source_sig"] == "stale-old-sig"


def test_identity_verified_resight_skips_scan_and_decode(tmp_path, quiet, monkeypatch):
    """validation_source_sig == sig + sha256 identity -> straight to duplicate gate."""
    dock = tmp_path / "dock"
    media = tmp_path / "media"
    item = dock / "Some.Film.2026.1080p.WEBRip"
    item.mkdir(parents=True)
    payload = b"\x1aE\xdf\xa3" + b"verified-payload" * 64
    (item / "film.mkv").write_bytes(payload)
    media.mkdir()
    (media / "film.mkv").write_bytes(payload)
    monkeypatch.setattr(ap, "MEDIA", media)

    sig = ap.source_signature(ap.video_files_of(item))
    state = {item.name: {"validation_source_sig": sig, "source_sig": sig,
                         "security_validated": True}}
    deleted: list = []

    import unittest.mock as mock

    with (
        mock.patch.object(ap, "validate_media",
                          side_effect=AssertionError("must not re-scan verified payload")),
        mock.patch.object(ap, "decode_check",
                          side_effect=AssertionError("must skip decode for verified payload")),
        mock.patch.object(ap, "delete_verified_files",
                          side_effect=lambda _i, _v, _b: deleted.append(True) or True),
    ):
        ap.promote_item(item, state, dry=False)

    assert deleted == [True]
    assert state[item.name]["stage"] == "done"


def test_stale_paths_reaped(tmp_path, quiet):
    dock = tmp_path / "dock"
    dock.mkdir()
    (dock / "here-item").mkdir()
    state = {"gone-item": {"stage": "decode-failed"},
             "here-item": {"stage": "decode-checked"},
             "gone-ghost": {},
             "gone-done": {"stage": "done"},
             "gone-promoted": {"stage": "promoted"}}

    reaped = ap.reap_stale_states(state, dock=dock)

    assert reaped == 2
    assert "gone-item" not in state
    assert "gone-ghost" not in state
    assert "here-item" in state
    # success/progress history is preserved even when the dock path is gone
    assert state["gone-done"]["stage"] == "done"
    assert state["gone-promoted"]["stage"] == "promoted"
    assert any("gone-item" in line for line in quiet)


def test_clamscan_uses_unified_base_flags(mkv, tmp_path, monkeypatch):
    """Both branches share CLAMSCAN_BASE; the old alert-exceeds-max=yes is gone."""
    import inspect

    assert ap.CLAMSCAN_BASE == ["--no-summary", "--stdout", "--infected",
                                "--alert-exceeds-max=no",
                                "--max-filesize=0", "--max-scansize=0",
                                "--max-scantime=0"]
    src = inspect.getsource(ap.clamav_check)
    assert "--alert-exceeds-max=yes" not in src
    assert src.count("*CLAMSCAN_BASE") == 2

    argv_file = tmp_path / "argv.txt"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    scanner = bindir / "clamscan"
    scanner.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {argv_file}\nexit 0\n")
    scanner.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")

    ok, _ = ap.clamav_check(mkv)

    assert ok
    argv = argv_file.read_text().splitlines()
    for flag in ap.CLAMSCAN_BASE:
        assert flag in argv
