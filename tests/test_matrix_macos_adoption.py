from types import SimpleNamespace

import pytest

from tools import run_new_package_matrix as matrix


def test_macos_adoption_checks_command_and_pid_birth_before_polling(monkeypatch):
    birth = ["Sun Oct 11 21:00:00 2026"]
    command = ["python -m trievo_bench.l1_arena_compat --output /tmp/dsh-owned-run"]
    monkeypatch.setattr(matrix.sys, "platform", "darwin")
    def ps(args, **kwargs):
        return SimpleNamespace(returncode=0, stdout=(command[0] if "command=" in args else birth[0] + " S"))
    monkeypatch.setattr(matrix.subprocess, "run", ps)
    receipt = {"pid": 123, "output": "/tmp/dsh-owned-run",
               "process_start_ticks": "darwin:" + birth[0]}
    worker = matrix.AdoptedProcess(receipt)
    assert worker.poll() is None
    birth[0] = "Sun Oct 11 22:00:00 2026"
    assert worker.poll() == 0
    with pytest.raises(ValueError, match="no longer running"):
        matrix.AdoptedProcess(receipt)
    command[0] = "python -m trievo_bench.l1_arena_compat --output /tmp/other-run"
    with pytest.raises(ValueError, match="identity mismatch"):
        matrix.AdoptedProcess(receipt)
