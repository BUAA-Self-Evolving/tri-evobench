import pytest

from tools.run_new_package_matrix import matrix, restrict_plan_jobs


def test_selected_plan_defines_expected_coverage_and_keeps_source_order():
    rows = [dict(id=f"l1/reset/zcode/s{i:02}", level="l1", agent="zcode",
                 condition="reset", turns=["t1"] * i, output="unused") for i in range(1, 5)]
    selected = restrict_plan_jobs(rows, [rows[3]["id"], rows[0]["id"]])
    assert [r["id"] for r in selected] == [rows[0]["id"], rows[3]["id"]]
    assert len(rows) == 4
    assert matrix(selected)[0]["expected_turns"] == 5


@pytest.mark.parametrize("ids", [[], ["unknown"], ["a", "a"], [None], "a"])
def test_invalid_selected_matrix_plan_rejected_before_launch(ids):
    with pytest.raises(ValueError, match="unique existing"):
        restrict_plan_jobs([{"id": "a"}], ids)
