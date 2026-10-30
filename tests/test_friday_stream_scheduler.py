from __future__ import annotations

import json
from collections import deque

import pytest

from tools import run_new_package_matrix as matrix_driver
from tools.run_new_package_matrix import (
    _pop_eligible,
    assign_friday_credentials,
    friday_concurrency,
    friday_slot_capacities,
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


@pytest.mark.parametrize("configured", [[], {"unknown": 10}, {"FRIDAY_APP_ID_1": 0},
                                       {"FRIDAY_APP_ID_1": True}, {"FRIDAY_APP_ID_1": 1.5}])
def test_capacity_config_rejects_unknown_slots_and_invalid_limits(configured):
    with pytest.raises(ValueError, match="Friday"):
        friday_slot_capacities(_credentials(2), configured)


def test_capacity_defaults_and_reordering_follow_slot_names():
    credentials = _credentials(2)
    assert friday_slot_capacities(credentials, {"FRIDAY_APP_ID_2": 10}) == (1, 10)
    assert friday_slot_capacities(tuple(reversed(credentials)), {"FRIDAY_APP_ID_2": 10}) == (10, 1)


def test_heterogeneous_capacity_distributes_work_over_parallel_lanes():
    rows = [_row(f"stream-{index:02d}", 10) for index in range(60)]
    schedule = assign_friday_credentials(rows, _credentials(2), (10, 20))

    assert schedule["slot_estimated_work_units"] == [200, 400]
    assert schedule["slot_lane_work_units"] == [[20] * 10, [20] * 20]
    assert schedule["slot_affinity_group_counts"] == [20, 40]
    assert friday_concurrency(None, sum(schedule["slot_capacities"]), 60) == 30
    assert "secret" not in json.dumps(schedule)


def test_capacity_keeps_conditions_on_one_sequential_lane():
    rows = [_row("same", 10, condition=condition) for condition in ("reset", "frozen", "evolving")]
    schedule = assign_friday_credentials(rows, _credentials(1), (10,))
    assert len({(r["credential_slot"], r["credential_lane"]) for r in rows}) == 1
    assert friday_concurrency(None, 10, len(schedule["affinity_assignments"])) == 1
    queue = deque(rows[1:])
    assert _pop_eligible(queue, {1: 1}, {1: 10}, {(1, 1)}) is None


def test_capacity_resume_rejects_change_even_when_assignments_are_identical():
    credentials = _credentials(1)
    original = assign_friday_credentials([_row("a", 1)], credentials, (10,))
    changed = assign_friday_credentials([_row("a", 1)], credentials, (20,))
    assert original["affinity_assignments"] == changed["affinity_assignments"]
    with pytest.raises(ValueError, match="new --root"):
        validate_friday_schedule({"credential_schedule": original}, changed)
    legacy = assign_friday_credentials([_row("a", 1)], credentials)
    explicit_one = assign_friday_credentials([_row("a", 1)], credentials, (1,))
    assert legacy == explicit_one
    validate_friday_schedule({"credential_schedule": legacy}, explicit_one)
    with pytest.raises(ValueError, match="new --root"):
        validate_friday_schedule({"credential_schedule": legacy}, original)


def test_capacity_limits_are_enforced_even_with_unoccupied_lane():
    row = {"id": "a", "credential_slot": 1, "credential_lane": 3}
    queue = deque([row])
    assert _pop_eligible(queue, {1: 2}, {1: 2}, {(1, 1), (1, 2)}) is None


@pytest.mark.parametrize("global_limit", [3, 99])
def test_parallel_capacity_driver_reaches_limits_without_overlapping_lanes(tmp_path, monkeypatch, global_limit):
    from collections import Counter

    credentials = _credentials(2)
    rows = [
        {**_row(f"stream-{index:02d}", 1, condition=condition),
         "output": str(tmp_path / f"{index}-{condition}")}
        for index in range(12) for condition in ("reset", "frozen", "evolving")
    ]
    schedule = assign_friday_credentials(rows, credentials, (2, 4))
    active = Counter()
    peaks = Counter()
    lanes = set()
    launched = []
    completed = set()
    peak_total = 0
    by_id = {row["id"]: row for row in rows}

    class Child:
        def __init__(self, args, **kwargs):
            nonlocal peak_total
            row = by_id[args[0]]
            self.pid = len(launched) + 100
            self.slot = row["credential_slot"]
            self.lane = (self.slot, row["credential_lane"])
            assert self.lane not in lanes
            assert kwargs["env"]["FRIDAY_APP_ID"] == credentials[self.slot - 1].value
            condition = row["condition"]
            prior = {"frozen": "reset", "evolving": "frozen"}.get(condition)
            if prior:
                assert (row["flow_id"], prior) in completed
            self.identity = (row["flow_id"], condition)
            active[self.slot] += 1
            peak_total = max(peak_total, sum(active.values()))
            assert peak_total <= global_limit
            assert active[self.slot] <= (2, 4)[self.slot - 1]
            peaks[self.slot] = max(peaks[self.slot], active[self.slot])
            lanes.add(self.lane)
            launched.append(row["id"])
            self.polls = 0

        def poll(self):
            self.polls += 1
            if self.polls == 1:
                return None
            active[self.slot] -= 1
            lanes.remove(self.lane)
            completed.add(self.identity)
            return 0

    monkeypatch.setattr(matrix_driver, "latest_checkpoint", lambda row: None)
    monkeypatch.setattr(matrix_driver, "report_for", lambda row: (("done", {"ok": True}) if row.get("pid") else (None, None)))
    monkeypatch.setattr(matrix_driver, "command", lambda repo, row, *args: [row["id"]])
    monkeypatch.setattr(matrix_driver.subprocess, "Popen", Child)
    monkeypatch.setattr(matrix_driver, "process_start_ticks", lambda pid: str(pid))
    monkeypatch.setattr(matrix_driver.time, "sleep", lambda _: None)
    state = {"jobs": rows, "credential_schedule": schedule}
    assert matrix_driver.run_parallel(
        tmp_path, rows, rows, tmp_path, state, tmp_path / "status.json", global_limit,
        provider_mode="friday", credential_pool=credentials,
        credential_capacities={"FRIDAY_APP_ID_1": 2, "FRIDAY_APP_ID_2": 4},
    ) == 0
    if global_limit == 99:
        assert peaks == {1: 2, 2: 4}
    else:
        assert peak_total == 3
        assert {by_id[job]["credential_slot"] for job in launched[:3]} == {1, 2}
    assert len(launched) == 36
    assert not lanes
    assert all(slot["active_workers"] == 0 for slot in state["credential_slot_usage"])


def test_dry_run_reports_capacity_without_writing_root_or_launching(tmp_path, monkeypatch, capsys):
    import sys

    root = tmp_path / "must-not-exist"
    capacity_file = tmp_path / "capacity.json"
    capacity_file.write_text(json.dumps({"FRIDAY_APP_ID_1": 10}), encoding="utf-8")
    monkeypatch.setattr(matrix_driver, "provider_credentials", lambda *args: _credentials(1))
    monkeypatch.setattr(matrix_driver, "validate_generated_profile", lambda *args: None)
    monkeypatch.setattr(matrix_driver, "jobs", lambda *args, **kwargs: [_row(f"s{i}", 1) for i in range(12)])
    monkeypatch.setattr(sys, "argv", ["matrix", "--provider-mode", "friday", "--root", str(root),
                                     "--friday-capacities", str(capacity_file), "--dry-run"])
    def forbidden(*args, **kwargs):
        pytest.fail("dry run attempted mutation or worker launch")
    monkeypatch.setattr(matrix_driver, "save", forbidden)
    monkeypatch.setattr(matrix_driver.subprocess, "Popen", forbidden)
    assert matrix_driver.main() == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["concurrency"] == 10
    assert plan["selected_jobs"] == 12
    assert "secret" not in json.dumps(plan)
    assert not root.exists()


def test_assigning_default_plan_clears_previous_lane_metadata():
    rows = [_row(f"s{i}", 1) for i in range(4)]
    assign_friday_credentials(rows, _credentials(1), (2,))
    assert any(row["credential_lane"] == 2 for row in rows)
    assign_friday_credentials(rows, _credentials(1))
    assert not any("credential_lane" in row for row in rows)


def test_adoption_rejects_changed_lane_before_verifying_process(tmp_path, monkeypatch):
    row = {**_row("a", 1), "output": str(tmp_path / "a"), "credential_slot": 1, "credential_lane": 1}
    receipt = {**row, "credential_lane": 2}
    def forbidden(*args):
        pytest.fail("invalid lane reached process adoption")
    monkeypatch.setattr(matrix_driver, "AdoptedProcess", forbidden)
    with pytest.raises(ValueError, match="adopted credential slot/lane mismatch"):
        matrix_driver.run_parallel(
            tmp_path, [row], [row], tmp_path, {"adopted_rows": [receipt]},
            tmp_path / "status.json", 2, provider_mode="friday",
            credential_pool=_credentials(1), credential_capacities={"FRIDAY_APP_ID_1": 2},
        )
