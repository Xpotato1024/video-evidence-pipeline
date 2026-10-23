"""Drive this task's real Win32 controls; fixture engine owns all mutations."""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import time

import pytest
from test_native_setup import ENGINE, PAYLOAD, apply, input_for, invoke

GUI = os.environ.get("VEP_NATIVE_SETUP_GUI")
pytestmark = pytest.mark.skipif(
    os.name != "nt" or not GUI or not ENGINE or not PAYLOAD,
    reason="Windows native GUI fixtures not supplied",
)

if os.name == "nt":
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.GetDlgItem.argtypes = [ctypes.c_void_p, ctypes.c_int]
    user.GetDlgItem.restype = ctypes.c_void_p
    user.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
    user.SendMessageW.restype = ctypes.c_ssize_t
    user.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
    user.SetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    user.IsWindowEnabled.argtypes = [ctypes.c_void_p]
    user.IsWindowVisible.argtypes = [ctypes.c_void_p]
    user.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_ssize_t)
    user.EnumWindows.argtypes = [callback_type, ctypes.c_ssize_t]


def wait(predicate, *, timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.025)
    raise AssertionError("GUI condition timed out")


def find_window(pid):
    windows = []

    @callback_type
    def visit(window, _):
        owner = ctypes.c_ulong()
        user.GetWindowThreadProcessId(window, ctypes.byref(owner))
        if owner.value == pid and user.IsWindowVisible(window):
            windows.append(window)
        return True

    user.EnumWindows(visit, 0)
    return windows[0] if windows else None


class Screen:
    def __init__(self, root, fields, *, environment=None):
        self.process = subprocess.Popen(
            [
                GUI,
                "--sandbox-root",
                str(root),
                "--manifest",
                fields["manifest"],
                "--payload",
                fields["payload"],
                "--sha256",
                fields["manifest_sha256"],
            ],
            env=dict(os.environ, VEP_GUI_TEST_NO_ACTIVATE="1", **(environment or {})),
        )
        self.window = wait(lambda: find_window(self.process.pid), timeout=10)
        self.idle()

    def control(self, identity):
        return user.GetDlgItem(self.window, identity)

    def text(self, identity):
        buffer = ctypes.create_unicode_buffer(65537)
        # Cross-process EDIT contents require WM_GETTEXT (GetWindowText only
        # exposes another process's cached caption, often empty for an EDIT).
        user.SendMessageW(self.control(identity), 0xD, len(buffer), ctypes.addressof(buffer))
        return buffer.value

    def enabled(self, identity):
        return bool(user.IsWindowEnabled(self.control(identity)))

    def click(self, identity):
        assert self.enabled(identity), (identity, self.text(19))
        user.SendMessageW(self.control(identity), 0xF5, 0, 0)  # BM_CLICK

    def set(self, identity, value):
        buffer = ctypes.create_unicode_buffer(str(value))
        assert user.SendMessageW(self.control(identity), 0xC, 0, ctypes.addressof(buffer))

    def fields(self, fields):
        for identity, name in ((101, "manifest"), (102, "payload"), (103, "manifest_sha256")):
            self.set(identity, fields[name])

    def idle(self):
        try:
            wait(lambda: self.enabled(11), timeout=120)
        except AssertionError as error:
            raise AssertionError((self.text(19), self.text(18))) from error

    def details(self):
        return json.loads(self.text(18))

    def confirm(self):
        self.click(17)
        assert self.enabled(13)
        self.click(13)

    def plan(self, *, rollback=False):
        self.click(12 if rollback else 11)
        self.idle()
        result = self.details()
        assert result["status"] == "ok", result
        return result["value"]

    def close(self):
        if self.process.poll() is None:
            user.SendMessageW(self.window, 0x10, 0, 0)
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.idle()
                user.SendMessageW(self.window, 0x10, 0, 0)
                self.process.wait(timeout=10)


def test_gui_confirmation_plan_update_rollback_and_cli_agreement(tmp_path):
    root = tmp_path / "GUI 日本語 root"
    root.mkdir()
    first, second = input_for(tmp_path, "gui-one"), input_for(tmp_path, "gui-two")
    screen = Screen(root, first)
    try:
        screen.click(10)
        assert not list(root.iterdir())  # confirmation screen itself does not initialize
        screen.confirm()
        screen.idle()
        assert screen.details()["status"] == "ok"
        sentinel = root / "data/retained"
        sentinel.write_bytes(b"synthetic GUI data\n")
        _, cli = invoke(root, "plan", **first)
        assert screen.plan() == cli["value"]
        assert "Install / update: gui-one" in screen.text(20)
        assert first["manifest_sha256"] in screen.text(20)
        assert "Publisher: unauthenticated" in screen.text(20)
        assert not screen.enabled(13)
        screen.set(103, "0" * 64)
        assert not screen.enabled(13) and not screen.enabled(17)
        screen.set(103, first["manifest_sha256"])
        screen.plan()
        screen.confirm()
        observed = []
        deadline = time.monotonic() + 120
        while not screen.enabled(11) and time.monotonic() < deadline:
            observed.append(screen.text(19))
            time.sleep(0.025)
        screen.idle()
        assert any(
            phase in line
            for line in observed
            for phase in ("STAGE", "VERIFY", "INVENTORY", "CHECK")
        )
        gui_result = screen.details()
        _, status = invoke(root, "status")
        assert gui_result["value"]["active"] == status["value"]["active"]
        assert status["value"]["active"]["generation"] == 1
        screen.fields(second)
        screen.plan()
        screen.confirm()
        screen.idle()
        assert screen.details()["value"]["active"]["generation"] == 2
        screen.fields(first)
        _, cli_plan = invoke(root, "plan", **first)
        assert screen.plan(rollback=True) == cli_plan["value"]
        screen.confirm()
        screen.idle()
        assert screen.details()["value"]["active"]["receipt_id"] == "gui-one"
        assert screen.details()["value"]["active"]["generation"] == 3
        assert sentinel.read_bytes() == b"synthetic GUI data\n"
        assert (root / "app/versions/gui-one").is_dir()
        assert (root / "app/versions/gui-two").is_dir()
    finally:
        screen.close()


def test_gui_cancel_before_stdin_ready_is_retained(tmp_path):
    root = tmp_path / "early-cancel"
    root.mkdir()
    invoke(root, "init-sandbox")
    fields = input_for(tmp_path, "cancel-before-spawn")
    screen = Screen(root, fields, environment={"VEP_GUI_TEST_SPAWN_DELAY_MS": "400"})
    try:
        screen.plan()
        screen.confirm()
        screen.click(14)  # deterministic fixture delay keeps engine stdin unavailable
        screen.idle()
        result = screen.details()
        assert result["status"] == "cancelled" and result["code"] == "CANCELLED"
        _, status = invoke(root, "status")
        assert status["value"]["active"]["generation"] == 0
    finally:
        screen.close()


def test_gui_crash_queries_state_and_confirmed_repair_uses_engine(tmp_path):
    root = tmp_path / "GUI crash recovery"
    root.mkdir()
    invoke(root, "init-sandbox")
    sentinel = root / "data/retained"
    sentinel.write_bytes(b"retain after uncertain outcome\n")
    fields = input_for(tmp_path, "crash-recovery")
    screen = Screen(root, fields, environment={"VEP_SANDBOX_INTERRUPT": "after-replace"})
    try:
        screen.plan()
        screen.confirm()
        wait(lambda: "RECOVERY_REQUIRED" in screen.text(19), timeout=120)
        screen.idle()
        assert screen.details()["value"]["recovery_required"] is True
        screen.click(16)
        assert screen.details()["action"].startswith("Reconcile")
        screen.confirm()
        screen.idle()
        result = screen.details()
        assert result["status"] == "ok" and result["value"]["reconciled_journals"] == 1
        _, status = invoke(root, "status")
        assert status["value"]["recovery_required"] is False
        assert status["value"]["active"] == result["value"]["active"]
        assert sentinel.read_bytes() == b"retain after uncertain outcome\n"
    finally:
        screen.close()


def test_gui_stale_plan_reports_same_cli_code_without_second_activation(tmp_path):
    root = tmp_path / "stale-plan"
    root.mkdir()
    invoke(root, "init-sandbox")
    fields = input_for(tmp_path, "stale")
    screen = Screen(root, fields)
    try:
        plan = screen.plan()
        apply(root, fields)
        _, cli = invoke(root, "apply", ok=False, plan_id=plan["plan_id"], **fields)
        screen.confirm()
        screen.idle()
        assert screen.details()["code"] == cli["code"] == "PLAN_STALE"
        _, status = invoke(root, "status")
        assert status["value"]["active"]["generation"] == 1
        assert not screen.enabled(13)
    finally:
        screen.close()


@pytest.mark.skipif(
    not os.environ.get("VEP_GUI_PROTOCOL_PEER"), reason="compiled adversarial fixture not supplied"
)
@pytest.mark.parametrize("mode", ["malformed", "oversized"])
def test_gui_invalid_flooding_peer_closes_output_before_wait(tmp_path, monkeypatch, mode):
    # A copied GUI can see only the explicit synthetic sibling peer in this root.
    fixture = tmp_path / "fixture-bin"
    fixture.mkdir()
    gui = fixture / "vep-setup-gui.exe"
    shutil.copy2(GUI, gui)
    shutil.copy2(os.environ["VEP_GUI_PROTOCOL_PEER"], fixture / "vep-setup.exe")
    monkeypatch.setitem(globals(), "GUI", str(gui))
    fields = {"manifest": "synthetic", "payload": "synthetic", "manifest_sha256": "0" * 64}
    screen = Screen(tmp_path / mode, fields)
    try:
        screen.click(11)
        wait(lambda: '"fixture_peer": true' in screen.text(18), timeout=10)
        screen.idle()
        assert screen.details()["value"]["fixture_peer"] is True
        assert not screen.enabled(13)
    finally:
        screen.close()
