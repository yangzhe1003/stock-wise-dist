"""策略复盘 API 路由 — SSE 流式复盘 + 保存为笔记 + 历史查询。"""

from __future__ import annotations

import asyncio
import json
import re

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.core.response import fail, ok
from app.services.review_service import (
    available_clis,
    clear_validation_token,
    delete_review,
    get_review_detail,
    get_reviews_for_scan,
    get_validation_token,
    run_ai_review,
    save_review_as_note,
    scan_clis,
)

router = APIRouter(prefix="/strategy", tags=["strategy-review"])


# ------------------------------------------------------------------
# Pydantic 模型
# ------------------------------------------------------------------


class SaveReviewPayload(BaseModel):
    cli_tool: str
    content: str
    user_operations: str = ""


# ------------------------------------------------------------------
# CLI 列表
# ------------------------------------------------------------------


@router.get("/review/clis")
def list_clis():
    """获取本地已安装的 AI CLI 工具列表（缓存结果）。"""
    return ok(available_clis())


@router.get("/review/clis/scan")
def scan_available_clis():
    """实时扫描本地已安装的 AI CLI 工具。"""
    return ok(scan_clis())


# ------------------------------------------------------------------
# AI 复盘（一次性返回完整文本）
# ------------------------------------------------------------------


@router.post("/scan/{scan_id}/review")
async def run_review(
    scan_id: str,
    cli: str = Query(default="claude"),
    user_operations: str = Query(default=""),
):
    """运行 AI 策略复盘，返回完整 Markdown 文本。

    Query params:
      - cli: CLI 工具名，默认 claude。
      - user_operations: 用户今日操作记录（可选），用于 AI 结合分析。
    """
    parts: list[str] = []
    async for chunk in run_ai_review(scan_id, cli, user_operations):
        parts.append(chunk)

    full_text = "".join(parts)

    # 检查是否是服务端错误信息
    if full_text.startswith("错误："):
        clear_validation_token(scan_id)
        return fail(full_text, code=5000)

    # 验证 AI 响应是否包含验证标记（缺失表示 AI 返回了错误/拒绝/格式异常）
    token = get_validation_token(scan_id)
    if token is None:
        return fail("验证标记丢失，请联系管理员", code=5000)

    if token not in full_text:
        clear_validation_token(scan_id)
        return fail("AI 返回结果异常：缺少响应验证标记，可能是 CLI 返回了错误或格式不兼容", code=5000)

    # 去除验证标记，保留纯分析内容
    clean_text = full_text.replace(token, "", 1).lstrip("\n")

    # 清理已使用的标记
    clear_validation_token(scan_id)

    return ok({"content": clean_text, "cli": cli}, message="复盘完成")


# ------------------------------------------------------------------
# 查询历史复盘
# ------------------------------------------------------------------


@router.get("/scan/{scan_id}/reviews")
def list_reviews(scan_id: str):
    """获取指定扫描的所有历史复盘记录。"""
    reviews = get_reviews_for_scan(scan_id)
    return ok(reviews)


@router.get("/review/{review_id}")
def get_review(review_id: int):
    """获取单条复盘记录详情。"""
    review = get_review_detail(review_id)
    if review is None:
        return fail("复盘记录不存在", code=4004)
    return ok(review)


@router.delete("/review/{review_id}")
def remove_review(review_id: int):
    """删除复盘记录及其关联笔记。"""
    if not delete_review(review_id):
        return fail("复盘记录不存在", code=4004)
    return ok(None, message="复盘记录已删除")


# ------------------------------------------------------------------
# 保存为笔记
# ------------------------------------------------------------------


@router.post("/scan/{scan_id}/review/save")
def save_review(scan_id: str, payload: SaveReviewPayload):
    """将 AI 复盘内容保存为复盘笔记，并关联到策略复盘记录。

    Body:
      - cli_tool: 使用的 CLI 工具名
      - content: AI 生成的复盘内容（Markdown）
      - user_operations: 用户今日操作记录（可选）
    """
    if not payload.content.strip():
        return fail("复盘内容不能为空", code=4001)

    result = save_review_as_note(
        scan_id, payload.cli_tool, payload.content, payload.user_operations,
    )
    if result is None:
        return fail("保存失败：无法获取扫描数据", code=5000)

    return ok(result, message="复盘笔记已生成")


# ------------------------------------------------------------------
# 获取关联笔记
# ------------------------------------------------------------------


@router.get("/review/{review_id}/note")
def get_review_note(review_id: int):
    """获取复盘记录关联的笔记详情。"""
    from app.services.notes_service import get_note

    review = get_review_detail(review_id)
    if review is None:
        return fail("复盘记录不存在", code=4004)

    note_id = review.get("note_id")
    if not note_id:
        return fail("该复盘记录尚未关联笔记", code=4004)

    note = get_note(note_id)
    if note is None:
        return fail("关联笔记不存在", code=4004)

    return ok({"review": review, "note": note})
