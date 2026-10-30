import builtins
import pytest

from tools import run_new_package_matrix as m


class FakeGate:
    def __init__(self):
        self.healthy = True
        self.trips = []
        self.waits = 0
        self.recover_on_check = False

    def check(self):
        result = {"healthy": self.healthy, "reason": "ok" if self.healthy else "storage paused"}
        if not self.healthy and self.recover_on_check:
            self.healthy = True
        return result

    def trip(self, reason):
        self.trips.append(reason)
        self.healthy = False

    def wait(self):
        self.waits += 1
        self.healthy = True

    def is_storage_error(self, text):
        return "no space left on device" in text.lower() or "docker volume command failed" in text.lower() or "session storage" in text.lower()


class Child:
    def __init__(self, row_id, code):
        self.row_id = row_id
        self.pid = 1000 + len(row_id)
        self.code = code

    def poll(self):
        return self.code


def test_storage_gate_from_environment_default_is_disabled(monkeypatch):
    monkeypatch.delenv("TRIEVO_STORAGE_GUARD", raising=False)
    assert m.storage_gate_from_environment() is None


def test_storage_gate_from_environment_import_failure_is_not_silent(monkeypatch):
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "trievo_bench.storage_recovery":
            raise ImportError("missing storage gate")
        return original_import(name, *args, **kwargs)

    monkeypatch.setenv("TRIEVO_STORAGE_GUARD", "1")
    monkeypatch.setattr(builtins, "__import__", guarded_import)

    with pytest.raises(ImportError, match="missing storage gate"):
        m.storage_gate_from_environment()


def test_main_storage_guard_sets_env_and_fails_closed_when_factory_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("TRIEVO_API_KEY", "test-key")
    monkeypatch.setenv("TRIEVO_STORAGE_GUARD", "0")
    monkeypatch.setattr(m.sys, "argv", [
        "run_new_package_matrix.py",
        "--root", str(tmp_path / "run"),
        "--storage-guard",
        "--only", "job",
    ])
    monkeypatch.setattr(m, "jobs", lambda *args, **kwargs: [dict(id="job", output=str(tmp_path / "out"))])
    monkeypatch.setattr(m, "storage_gate_from_environment", lambda: None)

    with pytest.raises(SystemExit, match="StorageGate.from_environment"):
        m.main()
    assert m.os.environ["TRIEVO_STORAGE_GUARD"] == "1"


def test_storage_gate_pauses_admission_and_keeps_pending(tmp_path, monkeypatch):
    rows = [dict(id=str(i), output=str(tmp_path / str(i))) for i in range(3)]
    gate = FakeGate()
    gate.healthy = False
    gate.recover_on_check = True
    launched = []
    snapshots = []

    monkeypatch.setattr(m, "latest_checkpoint", lambda row: None)
    monkeypatch.setattr(m, "report_for", lambda row: ("done", {"ok": True}) if row.get("pid") else (None, None))
    monkeypatch.setattr(m, "command", lambda repo, row, checkpoint, config: [row["id"]])
    monkeypatch.setattr(m.subprocess, "Popen", lambda args, **kwargs: launched.append(args[0]) or Child(args[0], 0))
    monkeypatch.setattr(m, "process_start_ticks", lambda pid: str(pid * 10))
    monkeypatch.setattr(m.time, "sleep", lambda _: None)

    original_save = m.save

    def save(path, state):
        snapshots.append((state.get("status"), list(state.get("pending_job_ids", []))))
        original_save(path, state)

    monkeypatch.setattr(m, "save", save)

    state = {"jobs": rows}
    assert m.run_parallel(tmp_path, rows, rows, tmp_path, state, tmp_path / "status.json", 2, storage_gate=gate) == 0
    assert gate.waits == 0
    assert ("paused_storage", ["0", "1", "2"]) in snapshots
    assert state["target_concurrency"] == 2
    assert state["pending_count"] == 0
    assert launched == ["0", "1", "2"]
    assert all(row["status"] == "completed" for row in rows)


def test_storage_failure_defers_until_gate_recovers_then_retries(tmp_path, monkeypatch):
    rows = [dict(id="storage", output=str(tmp_path / "storage")), dict(id="other", output=str(tmp_path / "other"))]
    gate = FakeGate()
    gate.recover_on_check = True
    attempts = {"storage": 0, "other": 0}
    launched = []

    monkeypatch.setattr(m, "latest_checkpoint", lambda row: tmp_path / "checkpoint" if row["id"] == "storage" else None)

    def report_for(row):
        if row["id"] == "storage" and attempts["storage"] == 1 and row.get("status") == "running":
            raise RuntimeError("no space left on device")
        if row["id"] == "storage":
            return ("done", {"ok": True}) if attempts["storage"] > 1 and row.get("pid") else (None, None)
        return ("done", {"ok": True}) if row.get("pid") else (None, None)

    def command(repo, row, checkpoint, config):
        attempts[row["id"]] += 1
        launched.append((row["id"], attempts[row["id"]], str(checkpoint) if checkpoint else None))
        return [row["id"], str(attempts[row["id"]])]

    def popen(args, **kwargs):
        assert gate.healthy, "no admission using a pre-fault health snapshot"
        row_id, attempt = args[0], int(args[1])
        code = 1 if row_id == "storage" and attempt == 1 else 0
        return Child(row_id, code)

    monkeypatch.setattr(m, "report_for", report_for)
    monkeypatch.setattr(m, "matrix", lambda rows: [])
    monkeypatch.setattr(m, "command", command)
    monkeypatch.setattr(m.subprocess, "Popen", popen)
    monkeypatch.setattr(m, "process_start_ticks", lambda pid: str(pid * 10))
    monkeypatch.setattr(m.time, "sleep", lambda _: None)

    state = {"jobs": rows}
    assert m.run_parallel(tmp_path, rows, rows, tmp_path, state, tmp_path / "status.json", 2, "continue", storage_gate=gate) == 0
    assert gate.trips
    assert gate.waits == 0
    assert launched == [
        ("storage", 1, str(tmp_path / "checkpoint")),
        ("other", 1, None),
        ("storage", 2, str(tmp_path / "checkpoint")),
    ]
    assert rows[0]["status"] == "completed"
    assert rows[0]["retry_count"] == 1
    assert rows[0]["attempt_history"][1]["event"] == "deferred_storage"
    assert state["deferred_jobs"] == []
    assert rows[1]["status"] == "completed"


def test_unsafe_storage_recovery_blocks_job_but_continues_others(tmp_path, monkeypatch):
    unsafe = dict(id="unsafe", output=str(tmp_path / "unsafe"))
    other = dict(id="other", output=str(tmp_path / "other"))
    rows = [unsafe, other]
    (tmp_path / "unsafe" / "_checkpoints" / "x").mkdir(parents=True)
    (tmp_path / "unsafe" / "_checkpoints" / "x" / "completed_turns.json").write_text("[]")
    attempts = {"unsafe": 0, "other": 0}
    gate = FakeGate()
    gate.recover_on_check = True

    def latest_checkpoint(row):
        if row["id"] == "unsafe":
            raise ValueError("no safe L2 checkpoint preserves the latest Agent execution")
        return None

    def report_for(row):
        if row["id"] == "unsafe":
            raise RuntimeError("session storage could not be written")
        return ("done", {"ok": True}) if row.get("pid") else (None, None)

    def command(repo, row, checkpoint, config):
        attempts[row["id"]] += 1
        return [row["id"]]

    def popen(args, **kwargs):
        return Child(args[0], 1 if args[0] == "unsafe" else 0)

    monkeypatch.setattr(m, "latest_checkpoint", latest_checkpoint)
    monkeypatch.setattr(m, "report_for", report_for)
    monkeypatch.setattr(m, "matrix", lambda rows: [])
    monkeypatch.setattr(m, "command", command)
    monkeypatch.setattr(m.subprocess, "Popen", popen)
    monkeypatch.setattr(m, "process_start_ticks", lambda pid: str(pid * 10))
    monkeypatch.setattr(m.time, "sleep", lambda _: None)

    state = {"jobs": rows}
    assert m.run_parallel(tmp_path, rows, rows, tmp_path, state, tmp_path / "status.json", 2, "continue", storage_gate=gate) == 1
    assert unsafe["status"] == "blocked_recovery"
    assert other["status"] == "completed"
    assert attempts == {"unsafe": 0, "other": 1}
    assert state["status"] == "completed_with_errors"
    assert state["failed_job_ids"] == ["unsafe"]


def test_checkpoint_json_without_completed_turns_blocks_pre_model_recovery(tmp_path, monkeypatch):
    row = dict(id="unsafe", output=str(tmp_path / "unsafe"))
    checkpoint = tmp_path / "unsafe" / "_checkpoints" / "key" / "run" / "i0001-01"
    checkpoint.mkdir(parents=True)
    (checkpoint / "checkpoint.json").write_text("{}")
    row["error"] = "Traceback in _start_taskstream_volume_transport: no space left on device"

    assert m._is_pre_model_initialization_storage_failure(row) is False
    assert not m._defer_storage_recovery(row, 5, allow_pre_model_initialization=True)
    assert row["status"] == "blocked_recovery"


def test_true_pre_model_volume_failure_can_defer_without_checkpoint(tmp_path, monkeypatch):
    row = dict(id="init", output=str(tmp_path / "init"))
    row["error"] = "Traceback in _start_taskstream_volume_transport: no space left on device"

    monkeypatch.setattr(m, "latest_checkpoint", lambda row: None)

    assert m._is_pre_model_initialization_storage_failure(row) is True
    assert m._defer_storage_recovery(row, 5, allow_pre_model_initialization=True)
    assert row["status"] == "deferred_storage"
    assert row["recovery_mode"] == "pre_model_initialization"


def test_launch_storage_error_without_pre_model_marker_does_not_replay_without_checkpoint(tmp_path, monkeypatch):
    row = dict(id="job", output=str(tmp_path / "job"))
    gate = FakeGate()

    monkeypatch.setattr(m, "latest_checkpoint", lambda row: None)
    monkeypatch.setattr(m, "report_for", lambda row: (None, None))
    monkeypatch.setattr(m, "matrix", lambda rows: [])
    monkeypatch.setattr(m, "command", lambda repo, row, checkpoint, config: ["job"])
    monkeypatch.setattr(
        m.subprocess,
        "Popen",
        lambda args, **kwargs: (_ for _ in ()).throw(RuntimeError("docker volume command failed: no space left on device")),
    )
    monkeypatch.setattr(m.time, "sleep", lambda _: None)

    state = {"jobs": [row]}
    assert m.run_parallel(tmp_path, [row], [row], tmp_path, state, tmp_path / "status.json", 1, "continue", storage_gate=gate) == 1
    assert row["status"] == "blocked_recovery"
    assert row["recovery_reason"] == "missing safe storage recovery checkpoint"


def test_launch_pre_model_volume_failure_can_retry_without_checkpoint(tmp_path, monkeypatch):
    row = dict(id="job", output=str(tmp_path / "job"))
    gate = FakeGate()
    gate.recover_on_check = True
    attempts = 0

    monkeypatch.setattr(m, "latest_checkpoint", lambda row: None)
    monkeypatch.setattr(m, "matrix", lambda rows: [])
    monkeypatch.setattr(m, "command", lambda repo, row, checkpoint, config: ["job"])
    monkeypatch.setattr(m, "process_start_ticks", lambda pid: str(pid * 10))
    monkeypatch.setattr(m.time, "sleep", lambda _: None)

    def report_for(row):
        return ("done", {"ok": True}) if row.get("pid") else (None, None)

    def popen(args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("Traceback in _start_taskstream_volume_transport: no space left on device")
        return Child("job", 0)

    monkeypatch.setattr(m, "report_for", report_for)
    monkeypatch.setattr(m.subprocess, "Popen", popen)

    state = {"jobs": [row]}
    assert m.run_parallel(tmp_path, [row], [row], tmp_path, state, tmp_path / "status.json", 1, "continue", storage_gate=gate) == 0
    assert attempts == 2
    assert row["status"] == "completed"
    assert row["recovery_mode"] == "pre_model_initialization"


def test_recover_storage_errors_reclassifies_only_storage_paused_rows(tmp_path, monkeypatch):
    storage = dict(id="storage", output=str(tmp_path / "storage"), status="paused_error", error="docker volume command failed: no space left on device")
    other = dict(id="other", output=str(tmp_path / "other"), status="paused_error", error="reference judge returned empty output")
    checkpoint = tmp_path / "checkpoint"

    monkeypatch.setattr(m, "latest_checkpoint", lambda row: checkpoint)

    m._prepare_recovered_storage_rows([storage, other], FakeGate(), 5)

    assert storage["status"] == "deferred_storage"
    assert storage["retry_count"] == 1
    assert storage["checkpoint"] == str(checkpoint)
    assert other["status"] == "paused_error"


def test_restore_prior_storage_state_requires_identity_and_keeps_evidence(tmp_path):
    row = {
        "id": "storage",
        "output": str(tmp_path / "storage"),
        "package": "pkg",
        "condition": "frozen",
        "turns": ["turn1"],
    }
    mismatched = {**row, "id": "mismatch"}
    prior_state = {
        "jobs": [
            {
                **row,
                "status": "paused_error",
                "log": "driver.log",
                "error": "docker volume command failed: no space left on device",
                "started_at": "old-start",
                "finished_at": "old-finish",
                "pid": 123,
            },
            {**mismatched, "output": str(tmp_path / "other"), "status": "paused_error"},
        ]
    }

    m._restore_prior_storage_state([row, mismatched], prior_state)

    assert row["status"] == "paused_error"
    assert row["log"] == "driver.log"
    assert row["error"].startswith("docker volume")
    assert row["attempt_history"][0]["event"] == "restored_prior_status"
    assert "status" not in mismatched


def test_clear_stale_attempt_error_before_relaunch():
    row = {
        "id": "storage",
        "error": "old no space left on device",
        "storage_error": "old storage",
        "recovery_error": "old unsafe",
        "returncode": 1,
        "finished_at": "old",
        "pid": 123,
        "process_start_ticks": "456",
        "attempt_history": [{"event": "kept"}],
    }

    m._clear_stale_attempt_error(row)

    for field in ("error", "storage_error", "recovery_error", "returncode", "finished_at", "pid", "process_start_ticks"):
        assert field not in row
    assert row["attempt_history"] == [{"event": "kept"}]


def test_storage_retry_limit_marks_needs_attention(tmp_path, monkeypatch):
    row = dict(id="storage", output=str(tmp_path / "storage"), retry_count=1)
    gate = FakeGate()

    monkeypatch.setattr(m, "latest_checkpoint", lambda row: tmp_path / "checkpoint")
    monkeypatch.setattr(m, "report_for", lambda row: (_ for _ in ()).throw(RuntimeError("docker volume command failed: ENOSPC")))
    monkeypatch.setattr(m, "matrix", lambda rows: [])
    monkeypatch.setattr(m, "command", lambda repo, row, checkpoint, config: [row["id"]])
    monkeypatch.setattr(m.subprocess, "Popen", lambda args, **kwargs: Child(args[0], 1))
    monkeypatch.setattr(m, "process_start_ticks", lambda pid: str(pid * 10))
    monkeypatch.setattr(m.time, "sleep", lambda _: None)

    state = {"jobs": [row]}
    assert m.run_parallel(tmp_path, [row], [row], tmp_path, state, tmp_path / "status.json", 1, "continue", storage_gate=gate, storage_retry_limit=1) == 1
    assert row["status"] == "needs_attention"
