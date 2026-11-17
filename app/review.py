"""待核候选判定（纯函数）：比较候选旧网格与基准发布旧网格。

触发暂存的三类变化（任一命中即待核）：
1. 旧格编号集合有变化（新增 / 缺失编号）；
2. 同编号旧格的矩形有变化；
3. 同编号旧格户数相对旧值变化超过 30%；
   旧值为零而新值非零同样触发（旧值、新值均为零不触发）。

本模块不触碰数据库与 Web 层，便于单元测试。
"""
from __future__ import annotations

from fractions import Fraction
from typing import Any

from .allocation import OldCell, _id_sort_key

# 严格“超过 30%”：变化比例 > 30/100 才触发，恰好 30% 不触发
CHANGE_RATIO_THRESHOLD = Fraction(30, 100)


def _household_change_reason(cell_id: str, old: int, new: int) -> dict[str, Any] | None:
    """同格户数变化判定，返回原因字典；不触发时返回 None。"""
    if old == 0:
        if new != 0:
            return {
                "type": "households_zero_to_nonzero",
                "cell_id": cell_id,
                "baseline_households": old,
                "candidate_households": new,
                "message": (
                    f"旧格 {cell_id} 户数旧值为 0，新值为 {new}（零值变非零），需人工核实"
                ),
            }
        return None
    if new == old:
        return None
    diff = abs(new - old)
    ratio = Fraction(diff, old)
    if ratio > CHANGE_RATIO_THRESHOLD:
        return {
            "type": "households_change_over_threshold",
            "cell_id": cell_id,
            "baseline_households": old,
            "candidate_households": new,
            "change_ratio": f"{float(ratio) * 100:.2f}%",
            "message": (
                f"旧格 {cell_id} 户数由 {old} 变为 {new}，"
                f"相对变化 {float(ratio) * 100:.2f}% 超过 30%，需人工核实"
            ),
        }
    return None


def compare_old_versions(
    candidate: list[OldCell], baseline: list[OldCell]
) -> list[dict[str, Any]]:
    """比较候选旧网格与基准发布旧网格，返回全部触发原因（无变化时为空列表）。"""
    reasons: list[dict[str, Any]] = []
    base_by_id = {c.id: c for c in baseline}
    cand_by_id = {c.id: c for c in candidate}
    base_ids = set(base_by_id)
    cand_ids = set(cand_by_id)

    for cid in sorted(cand_ids - base_ids, key=_id_sort_key):
        reasons.append(
            {
                "type": "cell_added",
                "cell_id": cid,
                "message": f"候选旧网格新增了基准中不存在的旧格 {cid}",
            }
        )
    for cid in sorted(base_ids - cand_ids, key=_id_sort_key):
        reasons.append(
            {
                "type": "cell_removed",
                "cell_id": cid,
                "message": f"候选旧网格缺少基准中的旧格 {cid}",
            }
        )
    for cid in sorted(base_ids & cand_ids, key=_id_sort_key):
        base_cell = base_by_id[cid]
        cand_cell = cand_by_id[cid]
        if base_cell.rect != cand_cell.rect:
            reasons.append(
                {
                    "type": "rect_changed",
                    "cell_id": cid,
                    "baseline_rect": list(base_cell.rect),
                    "candidate_rect": list(cand_cell.rect),
                    "message": (
                        f"旧格 {cid} 矩形由 {list(base_cell.rect)} "
                        f"变为 {list(cand_cell.rect)}"
                    ),
                }
            )
        reason = _household_change_reason(
            cid, base_cell.households, cand_cell.households
        )
        if reason is not None:
            reasons.append(reason)
    return reasons
