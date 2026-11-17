"""SQLite 持久化层。

设计要点：
- 输入按“整版提交”存为不可变版本（versions + cells），替换即产生新版本。
- 生成任务在创建（生成开始）的同一事务内固定 old/new 版本号与当前发布任务
  （baseline_generation，比较基准）；之后即使输入被替换，任务仍按该快照计算。
- 计算成功后，候选旧网格与基准发布的旧网格比较：编号/矩形变化、或同格户数
  相对变化超过 30%（零值变非零同算）时，完整结果暂存为待核候选（pending），
  不触碰 current_result；无变化或尚无发布结果时自动发布。
- 核准仅在“基准仍是当前发布 + 任务计算成功”时，于单事务内原子切换
  current_result；期间其他任务发布则候选过期（superseded）并拒绝核准；
  重复核准幂等，不重复写入。
- current_result 为单行表，任何失败路径都不写入、不半写，旧发布结果保持可查。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import allocation as alloc
from . import review as review_mod
from .config import DB_PATH, MAX_ITEMS_PER_SUBMISSION

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS versions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL CHECK (kind IN ('old', 'new')),
    created_at  TEXT NOT NULL,
    item_count  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS cells (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id   INTEGER NOT NULL REFERENCES versions(id),
    seq          INTEGER NOT NULL,
    cell_id      TEXT NOT NULL,
    x1           INTEGER NOT NULL,
    y1           INTEGER NOT NULL,
    x2           INTEGER NOT NULL,
    y2           INTEGER NOT NULL,
    area         INTEGER NOT NULL,
    households   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_cells_version ON cells(version_id);

CREATE TABLE IF NOT EXISTS generations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    status        TEXT NOT NULL CHECK (status IN ('pending', 'running', 'succeeded', 'failed')),
    old_version   INTEGER REFERENCES versions(id),
    new_version   INTEGER REFERENCES versions(id),
    error         TEXT,
    created_at    TEXT NOT NULL,
    started_at    TEXT,
    finished_at   TEXT,
    -- 创建任务时固定的“当前发布任务”（比较基准）；首次生成无发布时为 NULL
    baseline_generation INTEGER REFERENCES generations(id),
    -- none=无待核；pending=待核候选；approved=已核准发布；superseded=过期候选
    review_status TEXT NOT NULL DEFAULT 'none'
        CHECK (review_status IN ('none', 'pending', 'approved', 'superseded')),
    -- 待核触发原因（JSON 数组），无待核时为 NULL
    review_reasons TEXT
);

CREATE TABLE IF NOT EXISTS result_parts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    generation_id  INTEGER NOT NULL REFERENCES generations(id),
    district_id    TEXT NOT NULL,
    old_id         TEXT NOT NULL,
    overlap_area   INTEGER NOT NULL,
    households     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_parts_generation ON result_parts(generation_id);

-- 单行表：始终指向“当前已发布结果”
CREATE TABLE IF NOT EXISTS current_result (
    only_row       INTEGER PRIMARY KEY CHECK (only_row = 1),
    generation_id  INTEGER NOT NULL REFERENCES generations(id),
    published_at   TEXT NOT NULL,
    total_households INTEGER NOT NULL
);
"""


class NoVersionError(Exception):
    """缺少某一类输入的已提交版本。"""


class ValidationError(Exception):
    """输入业务校验失败（编号重复、内部重叠等）。"""


class ReviewError(Exception):
    """核准被拒绝（任务不可核准、候选已过期等），供路由层映射为 409。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# 旧库（无待核列的 generations 表）的增量迁移列
_MIGRATION_COLUMNS = {
    "baseline_generation": "INTEGER REFERENCES generations(id)",
    "review_status": (
        "TEXT NOT NULL DEFAULT 'none'"
    ),
    "review_reasons": "TEXT",
}


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """对已存在的旧表补齐新增列（CREATE TABLE IF NOT EXISTS 不会改旧表）。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(generations)")}
    altered = False
    for name, decl in _MIGRATION_COLUMNS.items():
        if name not in cols:
            conn.execute(f"ALTER TABLE generations ADD COLUMN {name} {decl}")
            altered = True
    if altered:
        conn.commit()


def get_conn(db_path: Path | None = None) -> sqlite3.Connection:
    path = Path(db_path or DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=30000;")
    # 保证任何连接（含后台工作线程）打开时表结构都已就绪（DDL 幂等）
    conn.executescript(SCHEMA)
    _migrate_schema(conn)
    return conn


def init_db(db_path: Path | None = None) -> None:
    conn = get_conn(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


# ---------- 输入版本 ----------

def _validate_items(kind: str, items: list[dict[str, Any]]) -> None:
    """业务校验：数量上限、编号唯一、同版矩形互不重叠。"""
    if not items:
        raise ValidationError("提交内容不能为空")
    if len(items) > MAX_ITEMS_PER_SUBMISSION:
        raise ValidationError(
            f"每版最多 {MAX_ITEMS_PER_SUBMISSION} 个条目，收到 {len(items)} 个"
        )
    ids = [it["id"] for it in items]
    if len(set(ids)) != len(ids):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        raise ValidationError(f"条目编号必须唯一，重复编号：{', '.join(dupes)}")
    rects = [(it["id"], it["rect"]) for it in items]
    try:
        alloc.assert_no_internal_overlap(rects)  # 抛 ValueError
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc


def insert_version(kind: str, items: list[dict[str, Any]], conn: sqlite3.Connection) -> int:
    """在给定连接/事务内写入一个不可变版本，返回版本号。"""
    _validate_items(kind, items)
    cur = conn.execute(
        "INSERT INTO versions(kind, created_at, item_count) VALUES (?, ?, ?)",
        (kind, _now(), len(items)),
    )
    version_id = cur.lastrowid
    for seq, it in enumerate(items):
        x1, y1, x2, y2 = it["rect"]
        conn.execute(
            "INSERT INTO cells(version_id, seq, cell_id, x1, y1, x2, y2, area, households)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                version_id,
                seq,
                it["id"],
                x1,
                y1,
                x2,
                y2,
                alloc.rect_area(it["rect"]),
                it.get("households"),
            ),
        )
    return version_id


def latest_version_id(kind: str, conn: sqlite3.Connection) -> int | None:
    row = conn.execute(
        "SELECT MAX(id) AS m FROM versions WHERE kind = ?", (kind,)
    ).fetchone()
    return row["m"]


def _load_version(conn: sqlite3.Connection, version_id: int) -> dict[str, Any]:
    vrow = conn.execute("SELECT * FROM versions WHERE id = ?", (version_id,)).fetchone()
    if vrow is None:
        raise NoVersionError(f"版本 {version_id} 不存在")
    cells = conn.execute(
        "SELECT * FROM cells WHERE version_id = ? ORDER BY seq", (version_id,)
    ).fetchall()
    return {
        "version": vrow["id"],
        "kind": vrow["kind"],
        "created_at": vrow["created_at"],
        "item_count": vrow["item_count"],
        "items": [
            {
                "id": c["cell_id"],
                "x1": c["x1"],
                "y1": c["y1"],
                "x2": c["x2"],
                "y2": c["y2"],
                "area": c["area"],
                "households": c["households"],
            }
            for c in cells
        ],
    }


def get_version(version_id: int, conn: sqlite3.Connection) -> dict[str, Any]:
    return _load_version(conn, version_id)


def list_versions(kind: str | None, conn: sqlite3.Connection) -> list[dict[str, Any]]:
    if kind:
        rows = conn.execute(
            "SELECT id FROM versions WHERE kind = ? ORDER BY id", (kind,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT id FROM versions ORDER BY id").fetchall()
    return [_load_version(conn, r["id"]) for r in rows]


# ---------- 生成任务 ----------

def create_generation(conn: sqlite3.Connection) -> int:
    """创建生成任务并在同一事务固定当前最新 old/new 版本快照与当前发布任务。

    - old/new 版本号在本事务内确定，此后替换任一输入不影响本任务；
    - baseline_generation 固定此刻 current_result 指向的发布任务，
      作为计算后“旧网格修订”比较基准；尚无发布结果时为 NULL。
    """
    old_v = latest_version_id("old", conn)
    new_v = latest_version_id("new", conn)
    missing = []
    if old_v is None:
        missing.append("旧网格")
    if new_v is None:
        missing.append("新片区")
    if missing:
        raise NoVersionError("缺少已提交的" + "、".join(missing) + "版本，无法生成")
    cur_row = conn.execute(
        "SELECT generation_id FROM current_result WHERE only_row=1"
    ).fetchone()
    baseline = cur_row["generation_id"] if cur_row is not None else None
    cur = conn.execute(
        "INSERT INTO generations(status, old_version, new_version, created_at,"
        " baseline_generation, review_status)"
        " VALUES ('pending', ?, ?, ?, ?, 'none')",
        (old_v, new_v, _now(), baseline),
    )
    return cur.lastrowid


def _load_snapshot(conn: sqlite3.Connection, gen_id: int) -> dict[str, Any]:
    grow = conn.execute("SELECT * FROM generations WHERE id = ?", (gen_id,)).fetchone()
    old_version = _load_version(conn, grow["old_version"])
    new_version = _load_version(conn, grow["new_version"])
    old_cells = [
        alloc.OldCell(
            id=c["id"],
            rect=(c["x1"], c["y1"], c["x2"], c["y2"]),
            households=c["households"],
        )
        for c in old_version["items"]
    ]
    districts = [
        alloc.NewDistrict(
            id=c["id"], rect=(c["x1"], c["y1"], c["x2"], c["y2"])
        )
        for c in new_version["items"]
    ]
    return {
        "row": grow,
        "old_version": old_version,
        "new_version": new_version,
        "old_cells": old_cells,
        "districts": districts,
    }


def _old_cells_from_version(version_id: int, conn: sqlite3.Connection) -> list[alloc.OldCell]:
    version = _load_version(conn, version_id)
    return [
        alloc.OldCell(
            id=c["id"],
            rect=(c["x1"], c["y1"], c["x2"], c["y2"]),
            households=c["households"],
        )
        for c in version["items"]
    ]


def _write_result_parts(
    conn: sqlite3.Connection, gen_id: int, allocations: list[alloc.Allocation]
) -> None:
    for a in allocations:
        conn.execute(
            "INSERT INTO result_parts(generation_id, district_id, old_id,"
            " overlap_area, households) VALUES (?, ?, ?, ?, ?)",
            (gen_id, a.district_id, a.old_id, a.overlap_area, a.households),
        )


def _mark_pending_superseded(conn: sqlite3.Connection, keep_gen_id: int) -> None:
    """发布发生后，其余待核候选全部过期（基准不再是当前发布）。"""
    conn.execute(
        "UPDATE generations SET review_status='superseded'"
        " WHERE review_status='pending' AND id <> ?",
        (keep_gen_id,),
    )


def _switch_current_result(
    conn: sqlite3.Connection, gen_id: int, total: int, published_at: str
) -> None:
    """单行 current_result 切换到指定任务（调用方负责事务边界）。"""
    conn.execute(
        "INSERT INTO current_result(only_row, generation_id, published_at,"
        " total_households) VALUES (1, ?, ?, ?) "
        "ON CONFLICT(only_row) DO UPDATE SET "
        "generation_id=excluded.generation_id,"
        " published_at=excluded.published_at,"
        " total_households=excluded.total_households",
        (gen_id, published_at, total),
    )


def run_generation(gen_id: int, db_path: Path | None = None) -> None:
    """工作线程入口：按任务固定的快照计算，再按待核规则发布或暂存；失败只标记任务。

    任何异常都会把任务落为 failed（而不是永久 running），
    且失败路径绝不写入 result_parts/current_result，旧发布结果保持可查。
    """
    conn = get_conn(db_path)
    try:
        # 1. 抢占任务并读取快照（任务版本号在创建时已固定，不受新提交影响）
        conn.execute(
            "UPDATE generations SET status='running', started_at=? "
            "WHERE id=? AND status='pending'",
            (_now(), gen_id),
        )
        conn.commit()
        snap = _load_snapshot(conn, gen_id)

        # 2. 纯函数计算（覆盖校验 + 最大余数分摊）
        try:
            allocations = alloc.allocate(snap["old_cells"], snap["districts"])
        except ValueError as exc:
            _mark_failed(conn, gen_id, str(exc))
            return

        total = sum(c.households for c in snap["old_cells"])
        allocated_total = sum(a.households for a in allocations)
        if allocated_total != total:
            # 理论上不会发生；作为守恒护栏，宁可失败也不发布错误结果
            _mark_failed(
                conn,
                gen_id,
                f"守恒校验失败：原总户数 {total}，分摊合计 {allocated_total}",
            )
            return

        grow = snap["row"]
        baseline_id = grow["baseline_generation"]

        # 3. 与基准发布任务的旧网格比较，决定自动发布还是暂存为待核候选。
        #    明细写入、任务状态、current_result 切换/候选标记全部在同一事务内，
        #    任一步失败整体回滚：不留半份明细，也不损坏旧发布结果。
        if baseline_id is None:
            # 尚无发布结果：照常发布
            with conn:
                _write_result_parts(conn, gen_id, allocations)
                conn.execute(
                    "UPDATE generations SET status='succeeded', error=NULL,"
                    " finished_at=?, review_status='none', review_reasons=NULL WHERE id=?",
                    (_now(), gen_id),
                )
                _switch_current_result(conn, gen_id, total, _now())
                _mark_pending_superseded(conn, gen_id)
            return

        baseline = conn.execute(
            "SELECT old_version FROM generations WHERE id=?", (baseline_id,)
        ).fetchone()
        if baseline is None:
            _mark_failed(conn, gen_id, f"比较基准任务 {baseline_id} 不存在")
            return
        baseline_cells = _old_cells_from_version(baseline["old_version"], conn)
        reasons = review_mod.compare_old_versions(snap["old_cells"], baseline_cells)

        with conn:
            _write_result_parts(conn, gen_id, allocations)
            if reasons:
                # 可疑旧网格修订：完整结果暂存为待核候选，不触碰 current_result
                conn.execute(
                    "UPDATE generations SET status='succeeded', error=NULL,"
                    " finished_at=?, review_status='pending', review_reasons=? WHERE id=?",
                    (_now(), json.dumps(reasons, ensure_ascii=False), gen_id),
                )
            else:
                # 无可疑变化：原子发布，并使其他待核候选过期
                conn.execute(
                    "UPDATE generations SET status='succeeded', error=NULL,"
                    " finished_at=?, review_status='none', review_reasons=NULL WHERE id=?",
                    (_now(), gen_id),
                )
                _switch_current_result(conn, gen_id, total, _now())
                _mark_pending_superseded(conn, gen_id)
    except Exception as exc:  # 兜底：任何意外都落 failed，不卡死任务
        try:
            _mark_failed(conn, gen_id, f"生成失败：{type(exc).__name__}: {exc}")
        except sqlite3.Error:
            pass
    finally:
        conn.close()


def _mark_failed(conn: sqlite3.Connection, gen_id: int, message: str) -> None:
    conn.execute(
        "UPDATE generations SET status='failed', error=?, finished_at=? WHERE id=?",
        (message, _now(), gen_id),
    )
    conn.commit()


def _row_to_generation(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    raw = data.get("review_reasons")
    data["review_reasons"] = json.loads(raw) if raw else None
    return data


def get_generation(gen_id: int, conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM generations WHERE id = ?", (gen_id,)).fetchone()
    return _row_to_generation(row) if row else None


def list_generations(
    conn: sqlite3.Connection, review_status: str | None = None
) -> list[dict[str, Any]]:
    if review_status:
        rows = conn.execute(
            "SELECT * FROM generations WHERE review_status=? ORDER BY id",
            (review_status,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM generations ORDER BY id").fetchall()
    return [_row_to_generation(r) for r in rows]


def approve_generation(
    gen_id: int, conn: sqlite3.Connection
) -> dict[str, Any]:
    """核准待核候选：条件满足时在单事务内原子切换供数结果。

    核准前提（全部满足才切换）：
    - 任务存在且计算成功（status='succeeded'，明细已完整暂存）；
    - 任务为待核候选（review_status='pending'）；
    - 比较基准仍是当前发布（baseline_generation == current_result.generation_id）。

    期间其他任务已发布 -> 候选标记为 superseded 并拒绝（过期核准）。
    重复核准已发布候选 -> 不重复写入，幂等返回 already_approved=True。
    失败不会留下半份明细或损坏旧结果。
    """
    grow = conn.execute(
        "SELECT * FROM generations WHERE id=?", (gen_id,)
    ).fetchone()
    if grow is None:
        raise ReviewError(f"生成任务 {gen_id} 不存在")
    gen = dict(grow)

    # 幂等：已核准发布过的任务，重复核准直接返回，不产生任何写入
    if gen["review_status"] == "approved":
        cur = conn.execute(
            "SELECT * FROM current_result WHERE only_row=1"
        ).fetchone()
        if cur is not None and cur["generation_id"] == gen_id:
            return _approval_response(conn, gen, already_approved=True)
        # 理论上不会出现 approved 但不指向当前结果的情况，防御性处理
        raise ReviewError("该任务已核准，但当前供数结果已被其他任务取代")

    if gen["status"] != "succeeded":
        raise ReviewError(
            f"任务计算未成功（当前状态 {gen['status']}），无法核准；"
            "只有计算成功的待核候选可以核准"
        )
    if gen["review_status"] == "superseded":
        raise ReviewError(
            "核准已过期：比较基准已不是当前发布结果，期间已有其他任务发布"
        )
    if gen["review_status"] != "pending":
        raise ReviewError("该任务不是待核候选，无需核准")

    cur = conn.execute("SELECT * FROM current_result WHERE only_row=1").fetchone()
    if cur is None or cur["generation_id"] != gen["baseline_generation"]:
        # 基准已被其他发布取代：先把候选落为过期（独立事务），再拒绝
        with conn:
            conn.execute(
                "UPDATE generations SET review_status='superseded' WHERE id=?",
                (gen_id,),
            )
        raise ReviewError(
            "核准已过期：比较基准已不是当前发布结果，期间已有其他任务发布"
        )

    total = sum(
        c["households"] for c in _load_version(conn, gen["old_version"])["items"]
    )
    # 原子切换：发布标记 + current_result 切换 + 其他候选过期，同一事务
    with conn:
        # 事务内最后复核一次基准，杜绝并发发布造成的错误切换
        cur2 = conn.execute(
            "SELECT generation_id FROM current_result WHERE only_row=1"
        ).fetchone()
        if cur2 is None or cur2["generation_id"] != gen["baseline_generation"]:
            raise ReviewError(
                "核准已过期：比较基准已不是当前发布结果，期间已有其他任务发布"
            )
        published_at = _now()
        conn.execute(
            "UPDATE generations SET review_status='approved' WHERE id=?", (gen_id,)
        )
        _switch_current_result(conn, gen_id, total, published_at)
        _mark_pending_superseded(conn, gen_id)

    final_row = conn.execute(
        "SELECT * FROM generations WHERE id=?", (gen_id,)
    ).fetchone()
    return _approval_response(conn, dict(final_row), already_approved=False)


def _approval_response(
    conn: sqlite3.Connection, gen: dict[str, Any], already_approved: bool
) -> dict[str, Any]:
    cur = conn.execute("SELECT * FROM current_result WHERE only_row=1").fetchone()
    raw = gen.get("review_reasons")
    return {
        "generation_id": gen["id"],
        "review_status": gen["review_status"],
        "already_approved": already_approved,
        "current_generation_id": cur["generation_id"],
        "published_at": cur["published_at"],
        "total_households": cur["total_households"],
        "review_reasons": json.loads(raw) if raw else None,
    }


def _build_result_for_generation(
    conn: sqlite3.Connection, gen_id: int, published_at: str | None
) -> dict[str, Any]:
    """组装指定任务的完整结果（供 /result 与候选查询共用）。"""
    grow = conn.execute("SELECT * FROM generations WHERE id=?", (gen_id,)).fetchone()
    if grow is None:
        raise ReviewError(f"生成任务 {gen_id} 不存在")
    old_version = _load_version(conn, grow["old_version"])
    new_version = _load_version(conn, grow["new_version"])

    parts = conn.execute(
        "SELECT * FROM result_parts WHERE generation_id=? ORDER BY id", (gen_id,)
    ).fetchall()
    by_district: dict[str, list[dict[str, Any]]] = {
        c["id"]: [] for c in new_version["items"]
    }
    for p in parts:
        by_district.setdefault(p["district_id"], []).append(
            {
                "district_id": p["district_id"],
                "old_id": p["old_id"],
                "overlap_area": p["overlap_area"],
                "households": p["households"],
            }
        )
    districts = []
    for d_id in sorted(by_district, key=alloc._id_sort_key):
        detail = sorted(by_district[d_id], key=lambda a: alloc._id_sort_key(a["old_id"]))
        districts.append(
            {
                "district_id": d_id,
                "allocated_households": sum(a["households"] for a in detail),
                "detail": detail,
            }
        )
    total = sum(c["households"] for c in old_version["items"])
    return {
        "generation_id": gen_id,
        "published_at": published_at,
        "total_households": total,
        "old_version": old_version,
        "new_version": new_version,
        "districts": districts,
    }


def get_candidate_result(
    gen_id: int, conn: sqlite3.Connection
) -> dict[str, Any] | None:
    """待核候选的完整暂存结果（附待核状态与触发原因）；任务不存在返回 None。"""
    grow = conn.execute("SELECT * FROM generations WHERE id=?", (gen_id,)).fetchone()
    if grow is None:
        return None
    result = _build_result_for_generation(conn, gen_id, None)
    result["review_status"] = grow["review_status"]
    result["review_reasons"] = (
        json.loads(grow["review_reasons"]) if grow["review_reasons"] else None
    )
    return result


def get_current_result(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """组装当前发布结果：片区汇总 + 两份输入版本。无发布时返回 None。"""
    cur = conn.execute("SELECT * FROM current_result WHERE only_row=1").fetchone()
    if cur is None:
        return None
    result = _build_result_for_generation(
        conn, cur["generation_id"], cur["published_at"]
    )
    # 与 current_result 中记录的总量保持一致（正常路径两者必然相等）
    result["total_households"] = cur["total_households"]
    return result
