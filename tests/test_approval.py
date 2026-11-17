"""待核候选与核准流程的 API 集成测试。

覆盖：首版自动发布、阈值触发暂存、核准原子切换、过期核准拒绝、
重复核准幂等、无变化自动发布、失败任务无半份明细、状态可查。
"""
from __future__ import annotations

from app import db as dbmod
from app.config import DB_PATH
from tests.conftest import poll_generation

OLD_BASE = {
    "items": [
        {"id": "g1", "rect": [0, 0, 10, 10], "households": 10},
        {"id": "g2", "rect": [0, 10, 10, 20], "households": 20},
    ]
}
NEW = {
    "items": [
        {"id": "d1", "rect": [0, 0, 5, 20]},
        {"id": "d2", "rect": [5, 0, 10, 20]},
    ]
}


def _submit(client, path, payload):
    r = client.post(path, json=payload)
    assert r.status_code == 201, r.text
    return r.json()


def _gen(client):
    r = client.post("/generations")
    assert r.status_code == 202, r.text
    return poll_generation(client, r.json()["id"])


def _old(g1_h=10, g2_h=20, ids=("g1", "g2")):
    return {
        "items": [
            {"id": ids[0], "rect": [0, 0, 10, 10], "households": g1_h},
            {"id": ids[1], "rect": [0, 10, 10, 20], "households": g2_h},
        ]
    }


def test_first_generation_auto_published_without_baseline(client):
    _submit(client, "/old-grids", OLD_BASE)
    _submit(client, "/new-districts", NEW)
    gen = _gen(client)
    assert gen["status"] == "succeeded"
    assert gen["baseline_generation"] is None
    assert gen["review_status"] == "none"
    assert gen["review_reasons"] is None
    assert client.get("/result").status_code == 200


def test_identical_old_version_auto_publishes(client):
    _submit(client, "/old-grids", OLD_BASE)
    _submit(client, "/new-districts", NEW)
    g1 = _gen(client)
    # 再提交一份完全相同的旧网格并重新生成 -> 无变化，自动发布
    _submit(client, "/old-grids", OLD_BASE)
    g2 = _gen(client)
    assert g2["baseline_generation"] == g1["id"]
    assert g2["review_status"] == "none"
    assert client.get("/result").json()["generation_id"] == g2["id"]


def test_household_change_over_threshold_staged_with_reasons(client):
    _submit(client, "/old-grids", OLD_BASE)
    _submit(client, "/new-districts", NEW)
    g1 = _gen(client)

    # g1: 10 -> 14，变化 40% > 30%，触发；g2 不变
    _submit(client, "/old-grids", _old(g1_h=14))
    g2 = _gen(client)
    assert g2["review_status"] == "pending"
    assert g2["baseline_generation"] == g1["id"]
    reasons = g2["review_reasons"]
    assert len(reasons) == 1
    assert reasons[0]["type"] == "households_change_over_threshold"
    assert reasons[0]["cell_id"] == "g1"

    # 暂存期间当前结果仍是 g1
    assert client.get("/result").json()["generation_id"] == g1["id"]

    # 待核列表可查
    pending = client.get("/generations", params={"review_status": "pending"}).json()
    assert [g["id"] for g in pending] == [g2["id"]]

    # 候选完整结果可查且带原因
    cand = client.get(f"/generations/{g2['id']}/result").json()
    assert cand["review_status"] == "pending"
    assert cand["total_households"] == 34
    assert cand["review_reasons"][0]["cell_id"] == "g1"

    # 核准 -> 原子切换
    r = client.post(f"/generations/{g2['id']}/approval")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["review_status"] == "approved"
    assert body["already_approved"] is False
    assert body["current_generation_id"] == g2["id"]
    assert client.get("/result").json()["generation_id"] == g2["id"]


def test_change_at_exactly_30_percent_auto_publishes(client):
    _submit(client, "/old-grids", OLD_BASE)
    _submit(client, "/new-districts", NEW)
    _gen(client)
    # g1: 10 -> 13 恰好 30%，不触发
    _submit(client, "/old-grids", _old(g1_h=13))
    gen = _gen(client)
    assert gen["review_status"] == "none"
    assert client.get("/result").json()["generation_id"] == gen["id"]


def test_zero_to_nonzero_staged(client):
    old_zero = _old(g1_h=0)
    _submit(client, "/old-grids", old_zero)
    _submit(client, "/new-districts", NEW)
    _gen(client)
    _submit(client, "/old-grids", _old(g1_h=1))
    gen = _gen(client)
    assert gen["review_status"] == "pending"
    assert gen["review_reasons"][0]["type"] == "households_zero_to_nonzero"


def test_rect_and_id_changes_staged(client):
    _submit(client, "/old-grids", OLD_BASE)
    _submit(client, "/new-districts", NEW)
    _gen(client)

    # 矩形变化（两格上下边界调整、彼此仍不重叠，且整体仍被 d1/d2 完整覆盖）
    changed_rect = {
        "items": [
            {"id": "g1", "rect": [0, 0, 10, 11], "households": 10},
            {"id": "g2", "rect": [0, 11, 10, 20], "households": 20},
        ]
    }
    _submit(client, "/old-grids", changed_rect)
    gen_rect = _gen(client)
    assert gen_rect["review_status"] == "pending"
    assert gen_rect["review_reasons"][0]["type"] == "rect_changed"

    # 编号变化
    _submit(client, "/old-grids", _old(ids=("h1", "g2")))
    gen_id = _gen(client)
    assert gen_id["review_status"] == "pending"
    types = {r["type"] for r in gen_id["review_reasons"]}
    assert types == {"cell_added", "cell_removed"}


def test_stale_approval_rejected_after_another_publication(client):
    _submit(client, "/old-grids", OLD_BASE)
    _submit(client, "/new-districts", NEW)
    g1 = _gen(client)  # 首版自动发布

    # 候选 A：户数大变 -> 待核
    _submit(client, "/old-grids", _old(g1_h=20))
    cand_a = _gen(client)
    assert cand_a["review_status"] == "pending"

    # 候选 B：基于 g1 创建，户数也大变 -> 待核；先核准 B
    _submit(client, "/old-grids", _old(g1_h=30))
    cand_b = _gen(client)
    assert cand_b["review_status"] == "pending"
    r = client.post(f"/generations/{cand_b['id']}/approval")
    assert r.status_code == 200
    assert client.get("/result").json()["generation_id"] == cand_b["id"]

    # 此时 A 的基准 g1 已不是当前发布 -> 过期核准被拒绝，A 标记 superseded
    r = client.post(f"/generations/{cand_a['id']}/approval")
    assert r.status_code == 409 and "过期" in r.json()["detail"]
    a_after = client.get(f"/generations/{cand_a['id']}").json()
    assert a_after["review_status"] == "superseded"
    # 旧结果未被损坏
    assert client.get("/result").json()["generation_id"] == cand_b["id"]

    # 对 superseded 候选再次核准依旧被拒绝
    r = client.post(f"/generations/{cand_a['id']}/approval")
    assert r.status_code == 409


def test_repeated_approval_is_idempotent_and_does_not_rewrite(client):
    _submit(client, "/old-grids", OLD_BASE)
    _submit(client, "/new-districts", NEW)
    _gen(client)
    _submit(client, "/old-grids", _old(g1_h=20))
    cand = _gen(client)

    r1 = client.post(f"/generations/{cand['id']}/approval")
    assert r1.status_code == 200 and r1.json()["already_approved"] is False
    published_at_1 = client.get("/result").json()["published_at"]

    r2 = client.post(f"/generations/{cand['id']}/approval")
    assert r2.status_code == 200
    body = r2.json()
    assert body["already_approved"] is True
    assert body["review_status"] == "approved"
    # 没有重复写入：发布时间不变，当前结果仍指向同一任务
    assert client.get("/result").json()["published_at"] == published_at_1
    assert client.get("/result").json()["generation_id"] == cand["id"]


def test_approval_rejected_for_non_candidate_and_failed(client):
    _submit(client, "/old-grids", OLD_BASE)
    _submit(client, "/new-districts", NEW)
    g1 = _gen(client)  # 自动发布，review_status=none

    # 核准一个自动发布（无待核）的任务 -> 409
    r = client.post(f"/generations/{g1['id']}/approval")
    assert r.status_code == 409 and "不是待核候选" in r.json()["detail"]

    # 核准不存在的任务 -> 404
    assert client.post("/generations/999/approval").status_code == 404

    # 失败任务 -> 409，且没有任何明细残留
    client.post(
        "/new-districts",
        json={"items": [{"id": "dX", "rect": [0, 10, 10, 20]}]},
    )
    failed = _gen(client)
    assert failed["status"] == "failed"
    r = client.post(f"/generations/{failed['id']}/approval")
    assert r.status_code == 409
    assert client.get(f"/generations/{failed['id']}/result").status_code == 409
    # 直连数据库：失败任务没有任何明细残留，current_result 未被损坏
    conn = dbmod.get_conn(DB_PATH)
    try:
        leftover = conn.execute(
            "SELECT COUNT(*) AS c FROM result_parts WHERE generation_id=?",
            (failed["id"],),
        ).fetchone()["c"]
        assert leftover == 0
        assert (
            conn.execute(
                "SELECT generation_id FROM current_result WHERE only_row=1"
            ).fetchone()["generation_id"]
            == g1["id"]
        )
    finally:
        conn.close()
    # 当前结果仍是首版
    assert client.get("/result").json()["generation_id"] == g1["id"]


def test_superseded_candidates_marked_on_auto_publication(client):
    _submit(client, "/old-grids", OLD_BASE)
    _submit(client, "/new-districts", NEW)
    g1 = _gen(client)

    # 候选 A 待核
    _submit(client, "/old-grids", _old(g1_h=20))
    cand_a = _gen(client)
    assert cand_a["review_status"] == "pending"

    # 提交与基准完全一致的旧网格生成 -> 自动发布，A 应同时过期
    _submit(client, "/old-grids", OLD_BASE)
    g_auto = _gen(client)
    assert g_auto["review_status"] == "none"
    a_after = client.get(f"/generations/{cand_a['id']}").json()
    assert a_after["review_status"] == "superseded"
    assert client.get("/result").json()["generation_id"] == g_auto["id"]
