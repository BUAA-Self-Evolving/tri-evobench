from __future__ import annotations

import json
from collections import deque

import pytest

from tools import run_new_package_matrix as matrix_driver
from tools.run_new_package_matrix import (
    _pop_eligible,
    assign_friday_credentials,
    friday_concurrency,
    job_environment,
    validate_friday_schedule,
)
from trievo_bench.provider_profiles import ProviderCredential


def _row(
    stream: str,
    turns: int,
    job: int = 0,
    *,
    agent: str = "hermes",
    condition: str = "evolving",
) -> dict:
    return {
        "id": f"l2/{condition}/{agent}/{stream}-{job}",
        "level": "l2",
        "flow_id": stream,
        "agent": agent,
        "condition": condition,
        "turns": [f"turn-{index}" for index in range(turns)],
    }


def _credentials(count: int) -> tuple[ProviderCredential, ...]:
    return tuple(
        ProviderCredential(f"FRIDAY_APP_ID_{index}", f"secret-{index}")
        for index in range(1, count + 1)
    )


def test_one_stream_runs_six_baselines_on_six_slots() -> None:
    agents = ("hermes", "skillopt", "trace2skill", "prime", "aah", "opencode")
    conditions = ("reset", "frozen", "evolving")
    rows = [
        _row("stream-a", 100, agent=agent, condition=condition)
        for condition in conditions
        for agent in agents
    ]

    schedule = assign_friday_credentials(rows, _credentials(6))

    baseline_slots = {
        agent: {row["credential_slot"] for row in rows if row["agent"] == agent}
        for agent in agents
    }
    assert all(len(slots) == 1 for slots in baseline_slots.values())
    assert len({next(iter(slots)) for slots in baseline_slots.values()}) == 6
    assert schedule["slot_estimated_work_units"] == [300] * 6
    assert schedule["strategy"] == "stream_baseline_affinity_lpt_turn_weighted_v2"
    assert "secret" not in json.dumps(schedule)


def test_conditions_keep_stream_baseline_affinity_and_order() -> None:
    rows = [
        _row("stream-a", 100, condition="evolving"),
        _row("stream-a", 100, condition="reset"),
        _row("stream-a", 100, condition="frozen"),
        _row("stream-a", 100, agent="skillopt", condition="evolving"),
    ]

    schedule = assign_friday_credentials(rows, _credentials(2))

    hermes = [row for row in rows if row["agent"] == "hermes"]
    assert len({row["credential_slot"] for row in hermes}) == 1
    assert [row["condition"] for row in sorted(
        hermes, key=lambda row: row["credential_job_order"]
    )] == ["reset", "frozen", "evolving"]
    assert rows[-1]["credential_slot"] != hermes[0]["credential_slot"]


def test_lpt_balances_turn_weighted_affinity_groups() -> None:
    rows = [_row("a", 8), _row("b", 7), _row("c", 6), _row("d", 5)]

    schedule = assign_friday_credentials(rows, _credentials(2))

    assert schedule["slot_estimated_work_units"] == [13, 13]
    assert schedule["slot_affinity_group_counts"] == [2, 2]


@pytest.mark.parametrize(
    ("requested", "credentials", "streams", "expected"),
    [(None, 8, 3, 3), (None, 2, 5, 2), (1, 8, 3, 1), (99, 2, 5, 2)],
)
def test_friday_concurrency_is_bounded_by_credentials_and_streams(
    requested, credentials, streams, expected,
) -> None:
    assert friday_concurrency(requested, credentials, streams) == expected


def test_active_slot_cannot_admit_second_job_but_other_slot_can() -> None:
    first = {"id": "a1", "credential_slot": 1}
    same_slot = {"id": "a2", "credential_slot": 1}
    other_slot = {"id": "b1", "credential_slot": 2}
    queue = deque([same_slot, other_slot])

    assert _pop_eligible(queue, {1}) == other_slot
    assert list(queue) == [same_slot]
    assert _pop_eligible(queue, {1}) is None
    assert list(queue) == [same_slot]
    assert first["credential_slot"] == 1


def test_job_environment_contains_only_selected_app_id() -> None:
    credentials = _credentials(2)
    row = {"id": "job", "credential_slot": 2}
    base = {
        "FRIDAY_APP_ID_1": "secret-1",
        "FRIDAY_APP_ID_2": "secret-2",
        "FRIDAY_APP_IDS": "secret-1\nsecret-2",
        "HTTPS_PROXY": "http://127.0.0.1:7890",
    }

    environment = job_environment("friday", credentials, row, base)

    assert environment["FRIDAY_APP_ID"] == "secret-2"
    assert environment["OPENAI_API_KEY"] == "secret-2"
    assert environment["TRIEVO_API_KEY"] == "secret-2"
    assert "FRIDAY_APP_IDS" not in environment
    assert "FRIDAY_APP_ID_1" not in environment
    assert "FRIDAY_APP_ID_2" not in environment
    assert "HTTPS_PROXY" not in environment


def test_resume_rejects_slot_reorder_or_changed_selection() -> None:
    rows = [_row("a", 8), _row("b", 3)]
    schedule = assign_friday_credentials(rows, _credentials(2))
    validate_friday_schedule({"credential_schedule": schedule}, schedule)

    reordered = assign_friday_credentials(
        [_row("a", 8), _row("b", 3)],
        tuple(reversed(_credentials(2))),
    )
    with pytest.raises(ValueError, match="differs"):
        validate_friday_schedule({"credential_schedule": schedule}, reordered)

    changed = assign_friday_credentials(
        [_row("a", 8), _row("b", 3), _row("c", 1)],
        _credentials(2),
    )
    with pytest.raises(ValueError, match="new --root"):
        validate_friday_schedule({"credential_schedule": schedule}, changed)


def test_parallel_driver_never_overlaps_jobs_on_one_app_id(tmp_path, monkeypatch) -> None:
    credentials = _credentials(2)
    rows = [
        {**_row("a", 1, 1), "output": str(tmp_path / "a1"), "credential_slot": 1},
        {**_row("a", 1, 2), "output": str(tmp_path / "a2"), "credential_slot": 1},
        {**_row("b", 1, 1), "output": str(tmp_path / "b1"), "credential_slot": 2},
    ]
    active_secrets = set()
    launched = []

    class Child:
        def __init__(self, args, **kwargs):
            self.pid = len(launched) + 100
            self.secret = kwargs["env"]["FRIDAY_APP_ID"]
            assert self.secret not in active_secrets
            active_secrets.add(self.secret)
            launched.append((args[0], self.secret))
            self.finished = False

        def poll(self):
            if not self.finished:
                active_secrets.remove(self.secret)
                self.finished = True
            return 0

    monkeypatch.setattr(matrix_driver, "latest_checkpoint", lambda row: None)
    monkeypatch.setattr(
        matrix_driver,
        "report_for",
        lambda row: (("done", {"ok": True}) if row.get("pid") else (None, None)),
    )
    monkeypatch.setattr(matrix_driver, "command", lambda repo, row, *args: [row["id"]])
    monkeypatch.setattr(matrix_driver.subprocess, "Popen", Child)
    monkeypatch.setattr(matrix_driver, "process_start_ticks", lambda pid: str(pid))
    monkeypatch.setattr(matrix_driver.time, "sleep", lambda _: None)

    state = {"jobs": rows}
    result = matrix_driver.run_parallel(
        tmp_path,
        rows,
        rows,
        tmp_path,
        state,
        tmp_path / "status.json",
        2,
        provider_mode="friday",
        credential_pool=credentials,
    )

    assert result == 0
    assert launched[0][1] != launched[1][1]
    assert launched[2][1] == "secret-1"
    assert not active_secrets
