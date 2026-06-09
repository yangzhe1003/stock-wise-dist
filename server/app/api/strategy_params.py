"""策略参数管理 API 路由。

提供策略参数的查询、手动调整、重置，以及因子有效性报告。
"""

from __future__ import annotations

import json

from fastapi import APIRouter
from pydantic import BaseModel

from app.core.response import fail, ok
from app.core.sqlite_storage import get_storage
from app.services.strategy_engine import (
    clear_params_cache,
    update_strategy_param,
    reset_strategy_params,
)

router = APIRouter(prefix="/strategy/params", tags=["strategy-params"])


# ------------------------------------------------------------------
# Pydantic 模型
# ------------------------------------------------------------------


class ParamUpdate(BaseModel):
    value: str  # JSON string 或纯数字字符串


# ------------------------------------------------------------------
# 参数查询
# ------------------------------------------------------------------


@router.get("/{strategy}")
def get_params(strategy: str):
    """获取指定策略的所有可调参数。"""
    if strategy not in ("s1", "s2", "s3"):
        return fail("策略不存在，请输入 s1/s2/s3", code=4004)

    storage = get_storage()
    params = storage.get_strategy_params(strategy)
    # 将 JSON current_value 解析为易读格式
    enriched = []
    for p in params:
        item = dict(p)
        try:
            item["current_parsed"] = json.loads(p["current_value"])
            item["default_parsed"] = json.loads(p["default_value"])
        except (json.JSONDecodeError, TypeError):
            item["current_parsed"] = p["current_value"]
            item["default_parsed"] = p["default_value"]
        # 解析调参历史
        try:
            item["tune_history"] = json.loads(p.get("tune_history_json", "[]"))
        except (json.JSONDecodeError, TypeError):
            item["tune_history"] = []
        enriched.append(item)

    return ok({
        "strategy": strategy,
        "params": enriched,
        "count": len(enriched),
    })


# ------------------------------------------------------------------
# 参数更新
# ------------------------------------------------------------------


@router.put("/{strategy}/{param_name}")
def update_param(strategy: str, param_name: str, payload: ParamUpdate):
    """手动调整单个策略参数的值。

    Body: {"value": "5.0"} 或 {"value": "[0,5,10,16,20]"}
    """
    if strategy not in ("s1", "s2", "s3"):
        return fail("策略不存在，请输入 s1/s2/s3", code=4004)

    # 验证 value 是合法 JSON
    try:
        parsed = json.loads(payload.value)
    except (json.JSONDecodeError, TypeError):
        return fail("value 必须是合法的 JSON 字符串", code=4001)

    storage = get_storage()
    success = storage.update_strategy_param(strategy, param_name, payload.value, "manual")

    if not success:
        return fail(f"参数 {param_name} 不存在于策略 {strategy}", code=4004)

    clear_params_cache()
    return ok({
        "strategy": strategy,
        "param_name": param_name,
        "new_value": payload.value,
    }, message=f"参数 {param_name} 已更新")


# ------------------------------------------------------------------
# 参数重置
# ------------------------------------------------------------------


@router.post("/{strategy}/reset")
def reset_params(strategy: str):
    """重置某策略所有参数为默认值。"""
    if strategy not in ("s1", "s2", "s3"):
        return fail("策略不存在，请输入 s1/s2/s3", code=4004)

    count = reset_strategy_params(strategy)
    return ok({"strategy": strategy, "reset_count": count},
              message=f"策略 {strategy} 的 {count} 个参数已重置为默认值")


# ------------------------------------------------------------------
# 因子有效性报告
# ------------------------------------------------------------------


@router.get("/effectiveness")
def get_effectiveness(strategy: str | None = None):
    """获取各策略各因子的历史有效性报告。

    可选参数 strategy=s1/s2/s3 筛选。
    """
    if strategy and strategy not in ("s1", "s2", "s3"):
        return fail("策略不存在", code=4004)

    storage = get_storage()
    factors = storage.get_factor_effectiveness(strategy)

    # 按策略分组
    grouped: dict[str, list] = {}
    for f in factors:
        s = f.get("strategy", "unknown")
        grouped.setdefault(s, []).append(f)

    return ok({
        "factors": factors,
        "grouped": grouped,
        "total": len(factors),
    })


# ------------------------------------------------------------------
# 所有策略参数概览
# ------------------------------------------------------------------


@router.get("/overview")
def get_all_params_overview():
    """获取三策略所有参数的概览（一次性查询）。"""
    storage = get_storage()
    result = {}
    for s in ["s1", "s2", "s3"]:
        params = storage.get_strategy_params(s)
        # 精简：只返回 name 和 current_value
        result[s] = [
            {"param_name": p["param_name"], "label": p["param_label"],
             "current": p["current_value"], "default": p["default_value"]}
            for p in params
        ]
    return ok(result)
