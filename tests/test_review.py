"""待核比较纯逻辑单元测试：编号/矩形变化、30% 户数阈值、零值变非零。"""
from __future__ import annotations

from app import review
from app.allocation import OldCell


def _cell(id_, rect, households):
    return OldCell(id_, rect, households)


def test_no_change_no_reasons():
    cells = [_cell("g1", (0, 0, 10, 10), 10)]
    assert review.compare_old_versions(cells, [_cell("g1", (0, 0, 10, 10), 10)]) == []


def test_cell_added_and_removed():
    baseline = [_cell("g1", (0, 0, 1, 1), 1), _cell("g2", (2, 0, 3, 1), 1)]
    candidate = [_cell("g1", (0, 0, 1, 1), 1), _cell("g3", (4, 0, 5, 1), 1)]
    reasons = review.compare_old_versions(candidate, baseline)
    types = {(r["type"], r["cell_id"]) for r in reasons}
    assert ("cell_added", "g3") in types
    assert ("cell_removed", "g2") in types


def test_rect_change_detected():
    baseline = [_cell("g1", (0, 0, 10, 10), 10)]
    candidate = [_cell("g1", (0, 0, 10, 11), 10)]
    reasons = review.compare_old_versions(candidate, baseline)
    assert len(reasons) == 1 and reasons[0]["type"] == "rect_changed"
    assert reasons[0]["baseline_rect"] == [0, 0, 10, 10]
    assert reasons[0]["candidate_rect"] == [0, 0, 10, 11]


def test_household_threshold_boundary_exactly_30_percent_not_triggered():
    # 旧值 10，新值 13：变化恰好 30%，不触发
    baseline = [_cell("g1", (0, 0, 1, 1), 10)]
    assert review.compare_old_versions(
        [_cell("g1", (0, 0, 1, 1), 13)], baseline
    ) == []
    # 新值 7：|7-10|/10 = 30%，同样不触发
    assert review.compare_old_versions(
        [_cell("g1", (0, 0, 1, 1), 7)], baseline
    ) == []


def test_household_change_over_30_percent_triggers():
    baseline = [_cell("g1", (0, 0, 1, 1), 10)]
    reasons = review.compare_old_versions(
        [_cell("g1", (0, 0, 1, 1), 14)], baseline
    )  # 40%
    assert len(reasons) == 1
    assert reasons[0]["type"] == "households_change_over_threshold"
    assert reasons[0]["change_ratio"] == "40.00%"

    reasons_down = review.compare_old_versions(
        [_cell("g1", (0, 0, 1, 1), 6)], baseline
    )  # -40%
    assert len(reasons_down) == 1
    assert reasons_down[0]["candidate_households"] == 6


def test_zero_old_value_nonzero_new_triggers_but_zero_to_zero_does_not():
    baseline = [_cell("g1", (0, 0, 1, 1), 0)]
    reasons = review.compare_old_versions(
        [_cell("g1", (0, 0, 1, 1), 1)], baseline
    )
    assert len(reasons) == 1 and reasons[0]["type"] == "households_zero_to_nonzero"

    assert review.compare_old_versions(
        [_cell("g1", (0, 0, 1, 1), 0)], baseline
    ) == []


def test_small_old_value_change_over_threshold():
    # 旧值 1，新值 2：变化 100% > 30%，触发
    baseline = [_cell("g1", (0, 0, 1, 1), 1)]
    reasons = review.compare_old_versions(
        [_cell("g1", (0, 0, 1, 1), 2)], baseline
    )
    assert len(reasons) == 1 and reasons[0]["type"] == "households_change_over_threshold"


def test_reasons_sorted_by_cell_id_and_multiple_reasons():
    baseline = [
        _cell("g1", (0, 0, 1, 1), 10),
        _cell("g2", (0, 0, 1, 1), 10),
    ]
    candidate = [
        _cell("g1", (0, 0, 2, 2), 20),  # 矩形 + 户数 100% 两个原因
        _cell("g2", (0, 0, 1, 1), 10),  # 无变化
    ]
    reasons = review.compare_old_versions(candidate, baseline)
    cell_ids = [r["cell_id"] for r in reasons]
    assert cell_ids == ["g1", "g1"]
    assert {r["type"] for r in reasons} == {
        "rect_changed",
        "households_change_over_threshold",
    }
