"""Actual user-mode Win32 client against the common JSONL transaction engine."""

import os

import pytest
from test_setup_gui import Screen
from test_user_install import ENGINE, PAYLOAD, call, install, installed_cli, payload_fields

pytestmark = pytest.mark.skipif(
    os.name != "nt" or not ENGINE or not PAYLOAD or not os.environ.get("VEP_NATIVE_SETUP_GUI"),
    reason="user-mode Windows GUI candidate not supplied",
)


def test_user_gui_license_confirmation_install_and_engine_plan_agreement(tmp_path):
    root = tmp_path / "GUI-user"
    fields = payload_fields(tmp_path, "user-gui-one")
    screen = Screen(root, fields, mode="user")
    try:
        screen.click(10)
        assert not root.exists()
        screen.confirm()
        screen.idle()
        assert screen.details()["status"] == "ok"
        screen.click(21)
        screen.idle()
        result, page = call(root, "licenses", **fields)
        assert result.returncode == 0
        assert screen.text(18).replace("\r", "") == page["value"]["text"].replace("\r", "")
        assert "third-party notices" in screen.text(18)
        assert "License text:" in screen.text(20)
        assert not screen.enabled(13)
        result, cli_plan = call(root, "plan", **fields)
        assert result.returncode == 0
        assert screen.plan() == cli_plan["value"]
        assert fields["manifest_sha256"] in screen.text(20)
        assert "Publisher: unauthenticated" in screen.text(20)
        screen.confirm()
        screen.idle()
        result = screen.details()
        assert result["status"] == "ok", result
        _, status = call(root, "status")
        assert status["value"]["active"] == result["value"]["active"]
        assert (root / "app/bin/vep.exe").is_file()
    finally:
        screen.close()


@pytest.mark.skipif(
    os.environ.get("VEP_USER_NORMAL_SMOKE") != "1",
    reason="normal unsigned build smoke not requested",
)
def test_normal_candidate_cli_install_and_gui_status_agree(tmp_path):
    root = tmp_path / "normal-user"
    assert call(root, "init-user", confirmed=True)[0].returncode == 0
    fields = payload_fields(tmp_path, "normal-smoke")
    assert install(root, fields)["value"]["active"]["generation"] == 1
    assert "Usage:" in installed_cli(root, "--help").stdout
    assert (
        '"installation_kind": "private-runtime"' in installed_cli(root, "doctor", "--json").stdout
    )
    screen = Screen(root, fields, mode="user")
    try:
        screen.click(15)
        screen.idle()
        _, status = call(root, "status")
        assert screen.details()["value"]["active"] == status["value"]["active"]
        _, plan = call(root, "plan", **fields)
        assert screen.plan() == plan["value"]
    finally:
        screen.close()


def test_user_gui_cancel_failure_and_repair_preserve_data(tmp_path):
    root = tmp_path / "gui-recovery"
    assert call(root, "init-user", confirmed=True)[0].returncode == 0
    sentinel = root / "data/keep.txt"
    sentinel.write_bytes(b"synthetic GUI retained data\n")
    fields = payload_fields(tmp_path, "user-gui-cancel")
    screen = Screen(root, fields, mode="user")
    try:
        screen.plan()
        screen.confirm()
        screen.click(14)
        screen.idle()
        result = screen.details()
        assert result["status"] == "cancelled" and result["code"] == "CANCELLED"
        assert call(root, "status")[1]["value"]["active"]["generation"] == 0
        screen.set(103, "0" * 64)
        screen.click(11)
        screen.idle()
        assert screen.details()["code"] == "MANIFEST_HASH_MISMATCH"
        assert not screen.enabled(13)
        screen.fields(fields)
        screen.click(16)
        screen.confirm()
        screen.idle()
        result = screen.details()
        assert result["status"] == "ok" and result["value"]["active"]["generation"] == 0
        assert sentinel.read_bytes() == b"synthetic GUI retained data\n"
    finally:
        screen.close()
