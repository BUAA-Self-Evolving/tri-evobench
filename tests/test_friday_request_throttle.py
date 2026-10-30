import json
import sqlite3

import pytest

from tools.throttle_friday_requests import reduce_limits
from trievo_bench.friday_gateway import SharedPacer
from trievo_bench.provider_profiles import ProviderCredential


def setup_pacer(tmp_path):
    credentials = (ProviderCredential("slot1", "secret-one"),
                   ProviderCredential("slot2", "secret-two"))
    pacer = SharedPacer(tmp_path / "budget.sqlite")
    for c in credentials:
        pacer.register(pacer.identity(c.value), 10, 2)
    return pacer, credentials


def reduce(pacer, credentials, tmp_path, **overrides):
    options = dict(rpm=5, max_inflight=1, expected_rpm=10,
                   expected_inflight=2, audit_dir=tmp_path / "audit",
                   reason="Test reduction", now=112)
    return reduce_limits(pacer.path, credentials, **(options | overrides))


def test_reduction_preserves_inflight_cooldown_and_spaces_next_request(tmp_path):
    pacer, credentials = setup_pacer(tmp_path)
    identity = pacer.identity(credentials[0].value)
    a, _ = pacer.try_acquire(identity, now=100, lease_seconds=100)
    b, _ = pacer.try_acquire(identity, now=106, lease_seconds=100)
    with sqlite3.connect(pacer.path) as db:
        db.execute("UPDATE budgets SET cooldown=130 WHERE identity=?", (identity,))
        leases_before = db.execute("SELECT * FROM leases ORDER BY id").fetchall()
    result = reduce(pacer, credentials, tmp_path)
    assert result["committed"] and result["before"][0]["active_leases"] == 2
    with sqlite3.connect(pacer.path) as db:
        assert db.execute("SELECT * FROM leases ORDER BY id").fetchall() == leases_before
        assert db.execute("SELECT cooldown FROM budgets WHERE identity=?", (identity,)).fetchone() == (130,)
    assert pacer.try_acquire(identity, now=124, lease_seconds=100)[0] is None
    pacer.release(a)
    assert pacer.try_acquire(identity, now=130, lease_seconds=100)[0] is None
    pacer.release(b)
    c, _ = pacer.try_acquire(identity, now=130, lease_seconds=100)
    assert c
    pacer.release(c)
    assert pacer.try_acquire(identity, now=141, lease_seconds=100)[0] is None
    assert pacer.try_acquire(identity, now=142, lease_seconds=100)[0]
    # Reconstructing a gateway from the old frozen policy must fail closed.
    with pytest.raises(ValueError, match="differs"):
        pacer.register(identity, 10, 2)
    assert "secret-one" not in (tmp_path / "audit/applied.json").read_text()
    assert json.loads((tmp_path / "audit/intent.json").read_text())["effective"]["rpm"] == 5


def test_mismatched_account_rolls_back_every_account(tmp_path):
    pacer, credentials = setup_pacer(tmp_path)
    with sqlite3.connect(pacer.path) as db:
        db.execute("UPDATE budgets SET rpm=9 WHERE identity=?", (pacer.identity(credentials[1].value),))
    with pytest.raises(ValueError, match="differ"):
        reduce(pacer, credentials, tmp_path)
    with sqlite3.connect(pacer.path) as db:
        assert db.execute("SELECT rpm FROM budgets WHERE identity=?", (pacer.identity(credentials[0].value),)).fetchone() == (10,)
    assert not (tmp_path / "audit").exists()


@pytest.mark.parametrize("options", [{"rpm": 11}, {"max_inflight": 3}, {"rpm": float("nan")},
                                     {"rpm": 0}, {"rpm": 10, "max_inflight": 2}])
def test_rejects_increases_and_invalid_limits(tmp_path, options):
    pacer, credentials = setup_pacer(tmp_path)
    with pytest.raises(ValueError):
        reduce(pacer, credentials, tmp_path, **options)


def test_existing_audit_directory_prevents_mutation(tmp_path):
    pacer, credentials = setup_pacer(tmp_path)
    (tmp_path / "audit").mkdir()
    with pytest.raises(FileExistsError):
        reduce(pacer, credentials, tmp_path)
    with sqlite3.connect(pacer.path) as db:
        assert db.execute("SELECT DISTINCT rpm,max_inflight FROM budgets").fetchall() == [(10, 2)]
