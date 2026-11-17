"""FastAPI 应用：输入版本提交、异步生成、结果查询。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Path as PathParam, Query
from fastapi.responses import RedirectResponse
from sqlite3 import Connection

from . import db
from .config import DB_PATH, WORKER_THREADS
from .models import (
    ApprovalOut,
    CandidateResultOut,
    CurrentResultOut,
    GenerationOut,
    HealthOut,
    NewDistrictSubmission,
    OldGridSubmission,
    ReviewStatus,
    VersionOut,
)

# 单线程工作池：生成任务按提交顺序串行执行与发布
_executor: ThreadPoolExecutor | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _executor
    db.init_db()
    _executor = ThreadPoolExecutor(
        max_workers=WORKER_THREADS, thread_name_prefix="generation"
    )
    yield
    _executor.shutdown(wait=True)
    _executor = None


app = FastAPI(
    title="城市统计片区重划分摊服务",
    description=(
        "整版提交旧网格（编号、整数矩形、户数）与新片区（编号、整数矩形），"
        "按重叠面积以最大余数法逐格分摊户数并守恒总户数。"
        "生成任务固定提交时刻的版本快照与当前发布任务；可疑旧网格修订的结果"
        "暂存为待核候选，统计员核准后才原子切换供数结果。"
    ),
    version="1.1.0",
    lifespan=lifespan,
)


def get_db() -> Connection:
    conn = db.get_conn()
    try:
        yield conn
    finally:
        conn.close()


def _items_from_old(payload: OldGridSubmission) -> list[dict]:
    return [
        {
            "id": it.id,
            "rect": it.rect.as_tuple(),
            "households": it.households,
        }
        for it in payload.items
    ]


def _items_from_new(payload: NewDistrictSubmission) -> list[dict]:
    return [{"id": it.id, "rect": it.rect.as_tuple()} for it in payload.items]


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/docs")


@app.get("/health", response_model=HealthOut, tags=["运维"])
def health(conn: Connection = Depends(get_db)):
    conn.execute("SELECT 1").fetchone()
    return {"status": "ok", "db": str(DB_PATH)}


@app.post("/old-grids", response_model=VersionOut, status_code=201, tags=["输入版本"])
def submit_old_grid(payload: OldGridSubmission, conn: Connection = Depends(get_db)):
    """整版提交旧网格；替换旧输入即产生新版本（历史版本保留）。"""
    try:
        with conn:
            version_id = db.insert_version("old", _items_from_old(payload), conn)
    except db.ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return db.get_version(version_id, conn)


@app.post(
    "/new-districts", response_model=VersionOut, status_code=201, tags=["输入版本"]
)
def submit_new_districts(
    payload: NewDistrictSubmission, conn: Connection = Depends(get_db)
):
    """整版提交新片区；替换旧输入即产生新版本（历史版本保留）。"""
    try:
        with conn:
            version_id = db.insert_version("new", _items_from_new(payload), conn)
    except db.ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return db.get_version(version_id, conn)


@app.get("/versions", response_model=list[VersionOut], tags=["输入版本"])
def list_versions(
    kind: str | None = Query(None, pattern="^(old|new)$"),
    conn: Connection = Depends(get_db),
):
    """列出输入版本，可按 kind=old/new 过滤。"""
    return db.list_versions(kind, conn)


@app.get("/versions/{version_id}", response_model=VersionOut, tags=["输入版本"])
def get_version(
    version_id: int = PathParam(..., ge=1), conn: Connection = Depends(get_db)
):
    try:
        return db.get_version(version_id, conn)
    except db.NoVersionError:
        raise HTTPException(status_code=404, detail=f"版本 {version_id} 不存在")


@app.post(
    "/generations",
    response_model=GenerationOut,
    status_code=202,
    tags=["生成任务"],
)
def create_generation(conn: Connection = Depends(get_db)):
    """开始一次生成：同步固定当前最新 old/new 版本快照与当前发布任务，后台串行计算。

    - 快照与比较基准在本请求事务内确定；此后替换任一输入不影响本任务；
    - 计算后若发现相对基准发布的可疑旧网格修订（编号/矩形变化，或同格户数
      相对变化超过 30%、零值变非零），完整结果暂存为待核候选（review_status=pending），
      触发原因可在任务详情中查询，等待人工核准；
    - 尚无发布结果，或无可疑变化时，任务完成即自动发布。
    """
    try:
        with conn:
            gen_id = db.create_generation(conn)
    except db.NoVersionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    assert _executor is not None
    _executor.submit(db.run_generation, gen_id, DB_PATH)
    return db.get_generation(gen_id, conn)


@app.get("/generations", response_model=list[GenerationOut], tags=["生成任务"])
def list_generations(
    review_status: ReviewStatus | None = Query(
        None,
        description="按待核状态过滤：pending=待核候选、approved=已核准、superseded=过期、none=无待核",
    ),
    conn: Connection = Depends(get_db),
):
    """列出全部生成任务，可按待核状态过滤（如 review_status=pending 查待核候选）。"""
    return db.list_generations(conn, review_status)


@app.get("/generations/{gen_id}", response_model=GenerationOut, tags=["生成任务"])
def get_generation(
    gen_id: int = PathParam(..., ge=1), conn: Connection = Depends(get_db)
):
    row = db.get_generation(gen_id, conn)
    if row is None:
        raise HTTPException(status_code=404, detail=f"生成任务 {gen_id} 不存在")
    return row


@app.post(
    "/generations/{gen_id}/approval",
    response_model=ApprovalOut,
    tags=["待核核准"],
)
def approve_generation(
    gen_id: int = PathParam(..., ge=1), conn: Connection = Depends(get_db)
):
    """统计员核准待核候选。

    仅当比较基准仍是当前发布、且任务计算成功时，才在单事务内原子切换供数结果；
    期间已有其他任务发布则拒绝过期核准；重复核准不重复写入（幂等）。
    """
    try:
        return db.approve_generation(gen_id, conn)
    except db.ReviewError as exc:
        message = str(exc)
        status = 404 if "不存在" in message else 409
        raise HTTPException(status_code=status, detail=message)


@app.get(
    "/generations/{gen_id}/result",
    response_model=CandidateResultOut,
    tags=["待核核准"],
)
def get_generation_result(
    gen_id: int = PathParam(..., ge=1), conn: Connection = Depends(get_db)
):
    """查询任意任务的完整结果；待核候选附待核状态与触发原因。

    任务不存在或计算未成功（无暂存明细）时返回 404/409。
    """
    row = db.get_generation(gen_id, conn)
    if row is None:
        raise HTTPException(status_code=404, detail=f"生成任务 {gen_id} 不存在")
    if row["status"] != "succeeded":
        raise HTTPException(
            status_code=409,
            detail=f"任务 {gen_id} 计算未成功（状态 {row['status']}），暂无结果",
        )
    result = db.get_candidate_result(gen_id, conn)
    assert result is not None
    return result


@app.get("/result", response_model=CurrentResultOut, tags=["发布结果"])
def get_current_result(conn: Connection = Depends(get_db)):
    """当前已发布结果（片区汇总 + 两份输入版本）。

    新结果完整发布前，这里始终返回上一份已发布结果；从未发布过时返回 404。
    """
    result = db.get_current_result(conn)
    if result is None:
        raise HTTPException(status_code=404, detail="尚无已发布的生成结果")
    return result
