from __future__ import annotations

from pydantic import BaseModel
from fastapi import APIRouter, Query

from app.core.response import fail, ok
from app.core.validation import normalize_code
from app.services.watchlist_service import (
    add_watchlist,
    create_category,
    delete_category,
    delete_watchlist,
    get_categories,
    get_watchlist,
    rename_category,
    update_watchlist_categories,
)

router = APIRouter(prefix="/watchlist", tags=["watchlist"])


class WatchlistCreate(BaseModel):
    code: str
    category_ids: list[int] | None = None


class CategoryCreate(BaseModel):
    name: str


class CategoryRename(BaseModel):
    name: str


# ---- 分类端点（静态路由必须在动态路由前）----


@router.get("/categories")
def list_categories():
    return ok(get_categories())


@router.post("/categories")
def add_category(payload: CategoryCreate):
    name = payload.name.strip()
    if not name:
        return fail("分类名不能为空", code=4001)
    if len(name) > 20:
        return fail("分类名不能超过 20 个字符", code=4002)
    cat = create_category(name)
    if cat is None:
        return fail("创建分类失败", code=5000)
    return ok(cat, message=f"已创建分类「{name}」")


@router.delete("/categories/{category_id}")
def remove_category(category_id: int):
    if not delete_category(category_id):
        return fail("分类不存在", code=4004)
    return ok(None, message="已删除分类")


@router.put("/categories/{category_id}")
def update_category(category_id: int, payload: CategoryRename):
    name = payload.name.strip()
    if not name:
        return fail("分类名不能为空", code=4001)
    if not rename_category(category_id, name):
        return fail("分类不存在", code=4004)
    return ok(None, message=f"已重命名为「{name}」")


# ---- 自选股端点 ----


@router.get("")
def list_watchlist(sort: str = "default", category_id: int | None = Query(default=None)):
    return ok(get_watchlist(sort=sort, category_id=category_id))


@router.post("")
def add(payload: WatchlistCreate):
    return ok(
        add_watchlist(normalize_code(payload.code), payload.category_ids),
        message="已加入自选",
    )


@router.delete("/{code}")
def delete(code: str):
    return ok(delete_watchlist(normalize_code(code)), message="已移出自选")


class UpdateCategoriesBody(BaseModel):
    category_ids: list[int]


@router.put("/{code}/categories")
def set_categories(code: str, payload: UpdateCategoriesBody):
    """更新某自选股的分类关联（替换式）。"""
    return ok(
        update_watchlist_categories(normalize_code(code), payload.category_ids),
        message="已更新分类",
    )
