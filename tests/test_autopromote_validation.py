"""Security and format gates for dock-autopromote."""

import importlib.util
import os
import shutil
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "dock-autopromote.py"
spec = importlib.util.spec_from_file_location("dock_autopromote", SCRIPT)
assert spec and spec.loader
ap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ap)


@pytest.fixture
def spiderman_ntb_pe(tmp_path):
    """Regression vector: the fake NTb Spider-Man release was a Windows PE."""
    path = tmp_path / "Spider-Man.Across.the.Spider-Verse.2023.1080p.WEB-DL.NTb.mkv"
    path.write_bytes(b"MZ" + b"\x00" * 126 + b"PE\x00\x00")
    return path


@pytest.fixture
def odyssey_kitsune_mkv(tmp_path):
    """Structurally valid media can still be the wrong film (Kitsune incident)."""
    path = tmp_path / "The.Odyssey.2026.1080p.WEB-DL.Kitsune.mkv"
    path.write_bytes(b"\x1aE\xdf\xa3" + b"\x00" * 128)
    return path


@pytest.fixture
def unknown_video_extension(tmp_path):
    path = tmp_path / "not-really-a-film.mp4"
    path.write_bytes(b"plain text wearing a video extension")
    return path


@pytest.fixture
def fake_clamscan(tmp_path, monkeypatch):
    """Real subprocess fixture with controllable ClamAV-compatible exit codes."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    scanner = bindir / "clamscan"
    scanner.write_text(
        "#!/bin/sh\n"
        "case \"${FAKE_CLAMSCAN_RESULT:-clean}\" in\n"
        "  clean) echo \"$5: OK\"; exit 0 ;;\n"
        "  infected) echo \"$5: Win.Test.EICAR_HDB-1 FOUND\"; exit 1 ;;\n"
        "  *) echo \"scanner database error\" >&2; exit 2 ;;\n"
        "esac\n"
    )
    scanner.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    return monkeypatch


def test_format_gate_rejects_spiderman_ntb_windows_executable(spiderman_ntb_pe):
    ok, detail = ap.format_gate(spiderman_ntb_pe)

    assert not ok
    assert "windows executable" in detail.lower()


def test_format_gate_accepts_structurally_valid_kitsune_media(odyssey_kitsune_mkv):
    ok, detail = ap.format_gate(odyssey_kitsune_mkv)

    assert ok
    assert "matroska" in detail.lower()


def test_format_gate_rejects_unknown_bytes_despite_video_extension(unknown_video_extension):
    ok, detail = ap.format_gate(unknown_video_extension)

    assert not ok
    assert "unrecognised" in detail.lower()


def test_clamav_gate_accepts_clean_file(odyssey_kitsune_mkv, fake_clamscan):
    ok, detail = ap.clamav_check(odyssey_kitsune_mkv)

    assert ok
    assert detail == "clean"


def test_clamav_gate_rejects_detected_malware(odyssey_kitsune_mkv, fake_clamscan):
    fake_clamscan.setenv("FAKE_CLAMSCAN_RESULT", "infected")

    ok, detail = ap.clamav_check(odyssey_kitsune_mkv)

    assert not ok
    assert "FOUND" in detail


def test_clamav_gate_fails_closed_on_scanner_error(odyssey_kitsune_mkv, fake_clamscan):
    fake_clamscan.setenv("FAKE_CLAMSCAN_RESULT", "error")

    ok, detail = ap.clamav_check(odyssey_kitsune_mkv)

    assert not ok
    assert "scanner error" in detail.lower()


def test_clamav_gate_fails_closed_when_scanner_missing(odyssey_kitsune_mkv, monkeypatch):
    monkeypatch.setenv("PATH", "")

    ok, detail = ap.clamav_check(odyssey_kitsune_mkv)

    assert not ok
    assert "not installed" in detail.lower()


def test_validate_media_stops_before_antivirus_for_bad_format(spiderman_ntb_pe, monkeypatch):
    def should_not_scan(_path):
        raise AssertionError("ClamAV must not run after the format gate rejects a file")

    monkeypatch.setattr(ap, "clamav_check", should_not_scan)

    status, detail = ap.validate_media(spiderman_ntb_pe)

    assert status == "format-failed"
    assert "windows executable" in detail.lower()


def test_validate_media_reports_malware(odyssey_kitsune_mkv, monkeypatch):
    monkeypatch.setattr(ap, "clamav_check", lambda _path: (False, "Eicar FOUND"))

    status, detail = ap.validate_media(odyssey_kitsune_mkv)

    assert status == "malware-detected"
    assert "FOUND" in detail


@pytest.mark.skipif(shutil.which("clamscan") is None, reason="ClamAV not installed")
def test_installed_clamav_detects_eicar_fixture(tmp_path):
    eicar = tmp_path / "eicar.com"
    eicar.write_bytes(
        b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
    )

    ok, detail = ap.clamav_check(eicar)

    assert not ok
    assert "FOUND" in detail
