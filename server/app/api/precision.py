"""精筛 API 路由。

提供精筛生成、查询、绩效统计、权重学习、参数优化等端点。
"""

from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.core.response import fail, ok
from app.core.sqlite_storage import get_storage
from app.services.precision_engine import PrecisionEngine

router = APIRouter(prefix="/precision", tags=["precision"])


def _engine() -> PrecisionEngine:
    return PrecisionEngine(get_storage())


# ------------------------------------------------------------------
# Pydantic 模型
# ------------------------------------------------------------------


class GenerateRequest(BaseModel):
    trade_date: str | None = None
    top_n: int = 5


class FeedbackRequest(BaseModel):
    code: str
    trade_date: str
    outcome: str  # win / loss / breakeven
    notes: str = ""


# ------------------------------------------------------------------
# 精筛生成与查询
# ------------------------------------------------------------------


@router.post("/generate")
def generate_picks(payload: GenerateRequest | None = None):
    """生成当日（或指定日期）的精选股票。

    需要当日三策略已完成扫描才有候选数据。
    """
    if payload is None:
        payload = GenerateRequest()

    trade_date = payload.trade_date
    if not trade_date:
        # 取最近一个有策略结果的交易日
        storage = get_storage()
        trade_date = storage.get_latest_precision_date()
        if not trade_date:
            # 尝试从策略扫描中获取
            for s in ["s1", "s2", "s3"]:
                dates = storage.get_strategy_dates(s, 1)
                if dates:
                    trade_date = dates[0]
                    break
        if not trade_date:
            return fail("无法确定交易日期，请先运行策略扫描", code=4001)

    engine = _engine()

    # ---- 自动更新所有 pending 的 outcome ----
    try:
        engine.update_outcomes()
    except Exception:
        pass

    # ---- 自动学习进化（累积足够新样本后触发） ----
    try:
        storage = get_storage()
        perf = storage.get_precision_performance()
        newly_judged = perf.get("judged", 0)

        # 判断是否需要学习：最近一次学习后新增了多少已判定样本
        last_log = storage.get_latest_precision_date()
        if last_log:
            log = storage.get_precision_log(last_log)
            if log:
                last_weights = log.get("weights_snapshot", {})
                if last_weights:
                    # 有权重快照说明学习过至少一次
                    pass

        if newly_judged >= 10:
            # 自动触发权重学习
            result = engine.learn_weights(min_samples=5)
            # 同时更新因子有效性
            engine.analyze_factor_effectiveness()

        if newly_judged >= 30:
            # 累积足够多，自动触发策略参数优化
            engine.learn_strategy_params(min_samples=10)
    except Exception:
        pass  # 学习失败不影响生成

    try:
        picks = engine.generate_picks(trade_date, payload.top_n)
        return ok({
            "trade_date": trade_date,
            "picks": picks,
            "count": len(picks),
        }, message=f"精筛完成，选出 {len(picks)} 只标的")
    except Exception as e:
        return fail(f"精筛生成失败: {e}", code=5000)


@router.get("/today")
def get_today_picks():
    """获取最新交易日的精筛结果。"""
    storage = get_storage()
    picks = storage.get_today_precision_picks()
    if not picks:
        return ok({"picks": [], "count": 0, "trade_date": None}, message="暂无精筛记录")
    return ok({
        "trade_date": picks[0].get("trade_date", ""),
        "picks": picks,
        "count": len(picks),
    })


@router.get("/history")
def get_history(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    outcome: str | None = Query(default=None),
):
    """分页获取历史精筛记录。可筛选 outcome (win/loss/breakeven/pending)。"""
    storage = get_storage()
    items, total = storage.get_precision_history(page, page_size, outcome)
    return ok({
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": max(1, (total + page_size - 1) // page_size),
    })


@router.get("/performance")
def get_performance():
    """获取精筛引擎整体绩效统计。"""
    storage = get_storage()
    perf = storage.get_precision_performance()
    return ok(perf)


# ------------------------------------------------------------------
# 权重管理
# ------------------------------------------------------------------


@router.get("/weights")
def get_weights():
    """获取当前所有信号权重。"""
    storage = get_storage()
    weights = storage.get_signal_weights()
    # 按类别分组
    grouped: dict[str, list] = {}
    for w in weights:
        cat = w.get("category", "other")
        grouped.setdefault(cat, []).append(w)
    return ok({"weights": weights, "grouped": grouped})


# ------------------------------------------------------------------
# 学习与优化
# ------------------------------------------------------------------


@router.post("/learn")
def trigger_learn():
    """手动触发权重学习（Bayesian 更新）。

    需要至少 30 个已判定的精筛样本。
    """
    engine = _engine()
    result = engine.learn_weights(min_samples=5)  # 先用低阈值方便测试
    if "error" in result:
        return fail(result["error"], code=4001)
    return ok(result, message=f"权重学习完成，更新了 {result.get('updated', 0)} 个信号")


@router.post("/learn/params")
def trigger_param_learning():
    """手动触发策略参数优化（网格搜索）。

    需要至少 50 个已判定的精筛样本。实际可用样本可能较少，
    可以用较低阈值进行初步优化。
    """
    engine = _engine()
    result = engine.learn_strategy_params(min_samples=10)
    if "error" in result:
        return fail(result["error"], code=4001)
    return ok(result, message=f"参数优化完成，调整了 {result.get('optimized_params', 0)} 个参数")


@router.post("/analyze-factors")
def trigger_factor_analysis():
    """分析各策略因子的历史有效性，更新 factor_effectiveness 表。"""
    engine = _engine()
    result = engine.analyze_factor_effectiveness()
    if "error" in result:
        return fail(result["error"], code=4001)
    return ok(result, message=f"因子分析完成，分析了 {result.get('analyzed_factors', 0)} 个因子")


# ------------------------------------------------------------------
# 反馈
# ------------------------------------------------------------------


@router.post("/feedback")
def submit_feedback(payload: FeedbackRequest):
    """手动标注精筛股的 outcome，用于纠正模型判断。"""
    if payload.outcome not in ("win", "loss", "breakeven"):
        return fail("outcome 必须是 win/loss/breakeven", code=4001)

    storage = get_storage()
    # 查找对应的精筛记录
    picks = storage.get_precision_picks(payload.trade_date)
    target = None
    for p in picks:
        if p["code"] == payload.code:
            target = p
            break

    if not target:
        return fail("未找到对应的精筛记录", code=4004)

    storage.set_precision_outcome(target["id"], payload.outcome)
    return ok(None, message=f"已将 {payload.code} 标注为 {payload.outcome}")


@router.post("/update-outcomes")
def trigger_update_outcomes():
    """更新所有 pending 精筛股的 latest_price 并自动判定 outcome。"""
    engine = _engine()
    engine.update_outcomes()
    return ok(None, message="Outcome 更新完成")
