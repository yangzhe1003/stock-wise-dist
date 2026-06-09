"""复盘笔记 API 路由。"""

from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel, field_validator

from app.core.response import fail, ok
from app.services.notes_service import (
    create_note,
    delete_note,
    export_notes,
    generate_ai_draft,
    get_calendar_dates,
    get_note,
    get_notes,
    get_notes_by_stock,
    get_stats,
    update_note,
)

router = APIRouter(prefix="/notes", tags=["notes"])


# ------------------------------------------------------------------
# Pydantic 模型
# ------------------------------------------------------------------


class NoteStock(BaseModel):
    code: str
    name: str = ""


class NoteCreate(BaseModel):
    title: str
    trade_date: str  # YYYY-MM-DD
    market_obs: str = ""
    trade_review: str = ""
    next_plan: str = ""
    tags: list[str] = []
    stocks: list[NoteStock] = []

    @field_validator("title")
    @classmethod
    def title_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("笔记标题不能为空")
        return v.strip()

    @field_validator("trade_date")
    @classmethod
    def date_format(cls, v: str) -> str:
        import re
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", v):
            raise ValueError("日期格式错误，应为 YYYY-MM-DD")
        return v


class NoteUpdate(BaseModel):
    title: str | None = None
    trade_date: str | None = None
    market_obs: str | None = None
    trade_review: str | None = None
    next_plan: str | None = None
    tags: list[str] | None = None
    stocks: list[NoteStock] | None = None


# ------------------------------------------------------------------
# 统计、日历、导出（静态路由必须在动态路由前）
# ------------------------------------------------------------------


@router.get("/stats")
def notes_stats():
    """获取复盘统计信息。"""
    return ok(get_stats())


@router.get("/ai-draft")
def notes_ai_draft(trade_date: str = Query(...)):
    """根据当日行情、策略信号、自选股表现生成复盘草稿。"""
    import re
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", trade_date):
        return fail("日期格式错误，应为 YYYY-MM-DD", code=4001)
    try:
        draft = generate_ai_draft(trade_date)
        return ok(draft, message=f"草稿已生成（{draft.get('source', '')}）")
    except Exception as e:
        return fail(f"生成草稿失败: {e}", code=5000)


@router.get("/calendar")
def notes_calendar(year: int = Query(...), month: int = Query(...)):
    """获取某月有笔记的日期列表。"""
    if month < 1 or month > 12:
        return fail("月份范围 1-12", code=4001)
    return ok(get_calendar_dates(year, month))


@router.get("/export")
def notes_export(fmt: str = Query(default="json", pattern=r"^(json|md)$")):
    """导出全部笔记。fmt: json | md"""
    data = export_notes(fmt)
    return ok(data)


@router.get("/by-stock/{stock_code}")
def notes_by_stock(
    stock_code: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """按标的代码反查关联笔记。"""
    return ok(get_notes_by_stock(stock_code, page, page_size))


# ------------------------------------------------------------------
# CRUD 端点
# ------------------------------------------------------------------


@router.get("")
def list_notes(
    trade_date: str | None = Query(default=None),
    tag: str | None = Query(default=None),
    keyword: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """获取笔记列表（分页、筛选）。"""
    return ok(get_notes(
        trade_date=trade_date,
        tag=tag,
        keyword=keyword,
        page=page,
        page_size=page_size,
    ))


@router.get("/{note_id}")
def get_single_note(note_id: int):
    """获取单篇笔记详情。"""
    note = get_note(note_id)
    if note is None:
        return fail("笔记不存在", code=4004)
    return ok(note)


@router.post("")
def add_note(payload: NoteCreate):
    """新建复盘笔记。"""
    note = create_note(
        title=payload.title,
        trade_date=payload.trade_date,
        market_obs=payload.market_obs,
        trade_review=payload.trade_review,
        next_plan=payload.next_plan,
        tags=payload.tags,
        stocks=[s.model_dump() for s in payload.stocks],
    )
    if note is None:
        return fail("创建笔记失败", code=5000)
    return ok(note, message="笔记已创建")


@router.put("/{note_id}")
def edit_note(note_id: int, payload: NoteUpdate):
    """更新复盘笔记。只更新传入的字段。"""
    # 检查笔记存在
    existing = get_note(note_id)
    if existing is None:
        return fail("笔记不存在", code=4004)

    kwargs = payload.model_dump(exclude_none=True)
    if "stocks" in kwargs:
        kwargs["stocks"] = [s.model_dump() if isinstance(s, NoteStock) else s for s in kwargs["stocks"]]

    note = update_note(note_id, **kwargs)
    if note is None:
        return fail("更新笔记失败", code=5000)
    return ok(note, message="笔记已更新")


@router.delete("/{note_id}")
def remove_note(note_id: int):
    """删除复盘笔记。"""
    if not delete_note(note_id):
        return fail("笔记不存在", code=4004)
    return ok(None, message="笔记已删除")
