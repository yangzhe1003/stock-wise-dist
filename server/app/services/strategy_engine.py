"""策略选股引擎。

提供三大独立策略的多因子打分：
- S1 趋势跟随 (Trend Following)
- S2 底部反转 (Bottom Reversal)
- S3 动量突破 (Momentum Breakout)

每只股票经过：
1. 排除过滤器（一票否决）
2. 市场环境判断（影响权重）
3. 因子连续打分（0-100）
4. 加权综合评分

所有打分函数接收单个 DataFrame（已含技术指标），返回评分结果。
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np
import pandas as pd

from app.core.sqlite_storage import get_storage
from app.services.technical_indicators import enrich_indicators


# ==========================================================================
# 数据模型
# ==========================================================================

@dataclass
class ScreenResult:
    """单只股票策略筛选结果。"""
    code: str
    name: str
    strategy: str                    # s1 / s2 / s3
    strategy_name: str               # 趋势跟随 / 底部反转 / 动量突破
    score: float                     # 综合得分 0-100
    factors_detail: dict[str, float]  # 各因子得分明细
    signals: list[str]               # 触发的信号描述
    metrics: dict[str, float]        # 关键指标值
    rank: int = 0


@dataclass
class MarketEnv:
    """市场环境描述。"""
    breadth_ratio: float             # 上涨占比 0-1
    index_change_pct: float          # 上证指数涨跌幅
    phase: str                       # 交易阶段
    trending: bool                   # 是否趋势市（涨跌比 > 55% 或 < 45%）
    bullish: bool                    # 是否偏多
    bearish: bool                    # 是否偏空

    @classmethod
    def from_overview(cls, overview: dict, phase: str = "morning") -> "MarketEnv":
        breadth = overview.get("breadth", {})
        total = breadth.get("up", 0) + breadth.get("flat", 0) + breadth.get("down", 0)
        ratio = breadth.get("up", 0) / max(total, 1)
        indices = overview.get("indices", [])
        sh_idx = next((i for i in indices if "上证" in i.get("name", "")), {})
        change_pct = sh_idx.get("changePct", 0)
        return cls(
            breadth_ratio=ratio,
            index_change_pct=change_pct,
            phase=phase,
            trending=ratio > 0.55 or ratio < 0.45,
            bullish=ratio > 0.5,
            bearish=ratio < 0.35,
        )


# ==========================================================================
# 策略参数加载
# ==========================================================================

# 内存缓存：{strategy: {param_name: parsed_value}}，避免每次打分都查DB
_params_cache: dict[str, dict[str, Any]] = {}
_params_cache_ts: float = 0.0
_PARAMS_CACHE_TTL = 60.0  # 60秒缓存


def _load_params_from_db(strategy: str) -> dict[str, Any]:
    """从数据库加载策略参数，返回 {param_name: parsed_value}。"""
    storage = get_storage()
    rows = storage.get_strategy_params(strategy)
    params: dict[str, Any] = {}
    for r in rows:
        try:
            params[r["param_name"]] = json.loads(r["current_value"])
        except (json.JSONDecodeError, TypeError):
            params[r["param_name"]] = r["current_value"]
    return params


def load_strategy_params(strategy: str) -> dict[str, Any]:
    """获取策略参数（带内存缓存）。"""
    global _params_cache, _params_cache_ts
    now = time.time()
    if now - _params_cache_ts > _PARAMS_CACHE_TTL:
        _params_cache = {}
        _params_cache_ts = now
    if strategy not in _params_cache:
        _params_cache[strategy] = _load_params_from_db(strategy)
    return _params_cache[strategy]


def get_param(params: dict[str, Any], key: str, default: Any) -> Any:
    """带默认值的参数读取。"""
    return params.get(key, default)


def clear_params_cache():
    """清除参数缓存（参数更新后调用）。"""
    global _params_cache, _params_cache_ts
    _params_cache = {}
    _params_cache_ts = 0.0


def update_strategy_param(strategy: str, param_name: str, new_value: Any):
    """更新单个策略参数并清除缓存。"""
    storage = get_storage()
    storage.update_strategy_param(strategy, param_name, json.dumps(new_value))
    clear_params_cache()


def reset_strategy_params(strategy: str):
    """重置策略所有参数为默认值并清除缓存。"""
    storage = get_storage()
    storage.reset_strategy_params(strategy)
    clear_params_cache()


# ==========================================================================
# 通用帮助函数
# ==========================================================================

def _latest(df: pd.DataFrame) -> pd.Series:
    """获取最新一行。"""
    return df.iloc[-1]


def _prev(df: pd.DataFrame, n: int = 1) -> pd.Series:
    """获取倒数第 n+1 行。"""
    return df.iloc[-1 - n] if len(df) > n else df.iloc[-1]


def _gaussian_score(value: float, center: float, sigma: float, max_score: float) -> float:
    """高斯型连续打分：越接近 center 得分越高。"""
    if sigma <= 0:
        return max_score
    return max_score * math.exp(-((value - center) ** 2) / (2 * sigma ** 2))


def _threshold_score(value: float, thresholds: list[tuple[float, float]]) -> float:
    """阶梯型连续打分：thresholds = [(阈值下限, 得分), ...], 按阈值从高到低排列。"""
    for threshold, score in thresholds:
        if value >= threshold:
            return score
    return 0.0


def _range_score(value: float, ranges: list[tuple[float, float, float]]) -> float:
    """区间打分：ranges = [(下界, 上界, 得分), ...]，匹配第一个命中的区间。"""
    for lo, hi, score in ranges:
        if lo <= value <= hi:
            return score
    return 0.0


# ==========================================================================
# 排除过滤器
# ==========================================================================

def _apply_exclusions(df: pd.DataFrame, code: str, name: str) -> str | None:
    """检查股票是否应被排除。返回 None 表示通过，返回字符串表示排除原因。"""
    if len(df) < 60:
        return "历史数据不足 60 个交易日"

    cur = _latest(df)

    # ST / 退市
    if "ST" in name or "退" in name:
        return "ST 或退市股"

    # 流动性检查：日均成交额
    recent_amounts = df["amount"].iloc[-20:]
    avg_amount = recent_amounts.mean() if not recent_amounts.empty else 0
    if avg_amount < 30_000_000:  # 3000 万
        return f"日均成交额过低 ({avg_amount/10000:.0f}万)"

    # 上市不足 60 个交易日
    if len(df) < 60:
        return "次新股 (< 60 天)"

    return None


# ==========================================================================
# S1 — 趋势跟随策略
# ==========================================================================

def score_s1_trend(df: pd.DataFrame, env: MarketEnv) -> dict:
    """S1：趋势跟随。

    在已确立的上升趋势中寻找回调买点。8 因子，满分 100。

    环境调整：熊市时降低权重（趋势策略在熊市表现差）。
    所有参数从 strategy_params 表读取，支持数据驱动优化。
    """
    params = load_strategy_params("s1")
    cur = _latest(df)
    detail: dict[str, float] = {}
    signals: list[str] = []

    # 环境调整系数
    env_bearish_mult = get_param(params, "env_bearish_mult", 0.8)
    env_mult = env_bearish_mult if env.bearish else 1.0

    # ---- 1. 多头排列强度 (20) ----
    bull_score = cur.get("ma_bull_score", 0)
    ma_map_raw = get_param(params, "ma_alignment_map", [0, 5, 10, 16, 20])
    ma_map = {i: v for i, v in enumerate(ma_map_raw)}
    detail["ma_alignment"] = round(ma_map.get(int(bull_score), 0) * env_mult, 1)
    if bull_score >= 3:
        signals.append("均线多头排列（5>10>20>60）")
    elif bull_score >= 2:
        signals.append("短期均线多头（5>10>20）")

    # ---- 2. 趋势健康度 (15) ----
    pct20 = cur.get("pct_chg20", 0)
    th_center = get_param(params, "trend_health_center", 5.0)
    th_sigma = get_param(params, "trend_health_sigma", 4.0)
    th_max = get_param(params, "trend_health_max", 15.0)
    if 0 <= pct20 <= 15:
        detail["trend_health"] = round(_gaussian_score(pct20, th_center, th_sigma, th_max) * env_mult, 1)
    elif pct20 > 15:
        detail["trend_health"] = round(max(0, th_max - (pct20 - 15) * 0.5) * env_mult, 1)
    else:
        detail["trend_health"] = 0
    if 3 <= pct20 <= 12:
        signals.append(f"20日趋势健康 +{pct20:.1f}%")

    # ---- 3. 回调到支撑 (15) ----
    close = cur.get("close", 0)
    ma10 = cur.get("ma10", 0)
    ma20 = cur.get("ma20", 0)
    dist_to_ma10 = (close - ma10) / ma10 * 100 if ma10 > 0 else 999
    dist_to_ma20 = (close - ma20) / ma20 * 100 if ma20 > 0 else 999

    pullback_tight = get_param(params, "pullback_ma10_tight", 1.0)
    pullback_near = get_param(params, "pullback_ma10_near", 3.0)
    pullback_scores = get_param(params, "pullback_scores", [15, 12, 9, 5, 4, 0])

    if close > ma10:
        if dist_to_ma10 < pullback_tight:
            detail["pullback_support"] = round(pullback_scores[0] * env_mult, 1)
            signals.append("紧贴 MA10 上方运行")
        elif dist_to_ma10 < pullback_tight * 2:
            detail["pullback_support"] = round(pullback_scores[1] * env_mult, 1)
        elif dist_to_ma10 < pullback_near:
            detail["pullback_support"] = round(pullback_scores[2] * env_mult, 1)
        elif dist_to_ma10 < pullback_near * 1.7:
            detail["pullback_support"] = round(pullback_scores[3] * env_mult, 1)
        else:
            detail["pullback_support"] = 0
    elif close > ma20:
        detail["pullback_support"] = round(pullback_scores[4] * env_mult, 1)
        signals.append("回调到 MA20 附近")
    else:
        detail["pullback_support"] = pullback_scores[5] if len(pullback_scores) > 5 else 0

    # ---- 4. MACD 趋势强度 (15) ----
    dif = cur.get("macd_dif", 0)
    dea = cur.get("macd_dea", 0)
    if dif > dea and dif > 0:
        detail["macd_strength"] = round(15 * env_mult, 1)
        signals.append("MACD 零轴上多头运行")
    elif dif > dea:
        detail["macd_strength"] = round(8 * env_mult, 1)
    elif dif > 0:
        detail["macd_strength"] = round(5 * env_mult, 1)
    else:
        detail["macd_strength"] = 0

    # ---- 5. 量价配合 (10) ----
    vol_ratio = cur.get("vol_ratio5", 1)
    vh_center = get_param(params, "vol_healthy_center", 1.4)
    vh_sigma = get_param(params, "vol_healthy_sigma", 0.4)
    if not np.isnan(vol_ratio):
        if 1.0 <= vol_ratio <= 2.0:
            detail["vol_healthy"] = round(_gaussian_score(vol_ratio, vh_center, vh_sigma, 10) * env_mult, 1)
        elif 0.7 <= vol_ratio < 1.0:
            detail["vol_healthy"] = round(5 * env_mult, 1)
        else:
            detail["vol_healthy"] = 0
        if 0.8 <= vol_ratio <= 2.5:
            signals.append(f"量价健康（量比{vol_ratio:.1f}）")
    else:
        detail["vol_healthy"] = 0

    # ---- 6. 布林带方向 (10) ----
    boll_slope = cur.get("boll_slope", 0)
    pct_b = cur.get("boll_pct_b", 0.5)
    score_boll = 0.0
    if boll_slope > 0:
        score_boll += 5
        signals.append("布林带中轨上移")
    if 0.5 <= pct_b <= 0.85:
        score_boll += 5
    elif 0.3 <= pct_b < 0.5:
        score_boll += 3
    detail["boll_direction"] = round(score_boll * env_mult, 1)

    # ---- 7. 相对强度 (10) ----
    pct5 = cur.get("pct_chg5", 0)
    if env.bullish:
        if pct5 > 3:
            detail["relative_strength"] = round(10 * env_mult, 1)
            signals.append(f"5日跑赢大盘 +{pct5:.1f}%")
        elif pct5 > 1:
            detail["relative_strength"] = round(7 * env_mult, 1)
        elif pct5 > 0:
            detail["relative_strength"] = round(4 * env_mult, 1)
        else:
            detail["relative_strength"] = 0
    else:
        if pct5 > 0:
            detail["relative_strength"] = round(10 * env_mult, 1)
            signals.append("逆市走强")
        elif pct5 > -1:
            detail["relative_strength"] = round(7 * env_mult, 1)
        elif pct5 > -3:
            detail["relative_strength"] = round(4 * env_mult, 1)
        else:
            detail["relative_strength"] = 0

    # ---- 8. 板块加分 (5) ----
    detail["sector_bonus"] = 0  # v1 暂不启用板块数据，后续版本接入

    # ---- 汇总 ----
    total = sum(detail.values())
    return {
        "score": round(min(total, 100), 1),
        "details": detail,
        "signals": signals,
    }


# ==========================================================================
# S2 — 底部反转策略
# ==========================================================================

def score_s2_reversal(df: pd.DataFrame, env: MarketEnv) -> dict:
    """S2：底部反转。

    在超卖区域寻找反转确认信号。7 因子，满分 100。

    环境调整：牛市时降低权重（普涨中不需要抄底），熊市时提升。
    所有参数从 strategy_params 表读取，支持数据驱动优化。
    """
    params = load_strategy_params("s2")
    cur = _latest(df)
    detail: dict[str, float] = {}
    signals: list[str] = []

    env_bullish_mult = get_param(params, "env_bullish_mult", 0.7)
    env_bearish_mult = get_param(params, "env_bearish_mult", 1.2)
    env_mult = env_bearish_mult if env.bearish else (env_bullish_mult if env.bullish and env.breadth_ratio > 0.7 else 1.0)

    close = cur.get("close", 0)

    # ---- 1. RSI 底背离 ----
    rsi_div_full = get_param(params, "rsi_divergence_full", 25.0)
    rsi_oversold_deep = get_param(params, "rsi_oversold_deep", 30.0)
    rsi_oversold_mild = get_param(params, "rsi_oversold_mild", 40.0)

    divergence = cur.get("rsi_divergence", 0)
    rsi = cur.get("rsi14", 50)
    if divergence == 1:
        detail["rsi_divergence"] = round(rsi_div_full * env_mult, 1)
        signals.append("RSI 底背离（反转信号）")
    elif not np.isnan(rsi) and rsi < rsi_oversold_deep:
        detail["rsi_divergence"] = round(15 * env_mult, 1)
        signals.append(f"RSI={rsi:.1f} 深度超卖")
    elif not np.isnan(rsi) and rsi < rsi_oversold_mild:
        detail["rsi_divergence"] = round(8 * env_mult, 1)
    else:
        detail["rsi_divergence"] = 0

    # ---- 2. 超卖程度 ----
    pct_b = cur.get("boll_pct_b", 0.5)
    oversold_scores = get_param(params, "oversold_scores", [20, 16, 10, 5, 0])
    oversold_thresholds = get_param(params, "boll_oversold_thresholds", [0.1, 0.2, 0.3, 0.4])
    if not np.isnan(pct_b):
        if pct_b <= oversold_thresholds[0]:
            detail["oversold_level"] = round(oversold_scores[0] * env_mult, 1)
            signals.append(f"股价触及布林下轨（%B={pct_b:.2f}）")
        elif pct_b <= oversold_thresholds[1]:
            detail["oversold_level"] = round(oversold_scores[1] * env_mult, 1)
        elif pct_b <= oversold_thresholds[2]:
            detail["oversold_level"] = round(oversold_scores[2] * env_mult, 1)
        elif pct_b <= oversold_thresholds[3]:
            detail["oversold_level"] = round(oversold_scores[3] * env_mult, 1)
        else:
            detail["oversold_level"] = oversold_scores[4] if len(oversold_scores) > 4 else 0
    else:
        detail["oversold_level"] = 0

    # ---- 3. 缩量止跌 ----
    vol_ratio = cur.get("vol_ratio5", 1)
    pct_chg = cur.get("pct_chg", 0)
    vol_dry_threshold = get_param(params, "volume_drying_threshold", 0.6)
    vol_dry_scores = get_param(params, "volume_drying_scores", [15, 10, 5, 0])

    if not np.isnan(vol_ratio):
        if vol_ratio < vol_dry_threshold and pct_chg > -2:
            detail["volume_drying"] = round(vol_dry_scores[0] * env_mult, 1)
            signals.append(f"缩量止跌（量比{vol_ratio:.1f}）")
        elif vol_ratio < 0.8:
            detail["volume_drying"] = round(vol_dry_scores[1] * env_mult, 1)
        elif vol_ratio < 1.0:
            detail["volume_drying"] = round(vol_dry_scores[2] * env_mult, 1)
        else:
            detail["volume_drying"] = vol_dry_scores[3] if len(vol_dry_scores) > 3 else 0
    else:
        detail["volume_drying"] = 0

    # ---- 4. 价格企稳 (15) ----
    ma5 = cur.get("ma5", 0)
    prev_ma5 = _prev(df).get("ma5", 0)
    lower_shadow = cur.get("lower_shadow", 0)
    score_stable = 0.0

    if close > ma5 and ma5 >= prev_ma5:
        score_stable += 10
        signals.append("站上 MA5 且 MA5 走平/上翘")
    elif close > ma5:
        score_stable += 6
    if not np.isnan(lower_shadow) and lower_shadow > 50:
        score_stable += 5
        signals.append("长下影线（支撑强劲）")
    elif not np.isnan(lower_shadow) and lower_shadow > 30:
        score_stable += 3
    detail["price_stabilizing"] = round(score_stable * env_mult, 1)

    # ---- 5. MACD 拐头 (10) ----
    cur_hist = cur.get("macd_hist", 0)
    prev_hist = _prev(df).get("macd_hist", 0)
    prev2_hist = _prev(df, 2).get("macd_hist", 0)

    if cur_hist < 0 and prev_hist < 0 and prev2_hist < 0:
        if cur_hist > prev_hist > prev2_hist:
            detail["macd_turning"] = round(10 * env_mult, 1)
            signals.append("MACD 绿柱连续3日收窄")
        elif cur_hist > prev_hist:
            detail["macd_turning"] = round(7 * env_mult, 1)
        else:
            detail["macd_turning"] = round(4 * env_mult, 1)
    elif cur_hist > 0 and prev_hist < 0:
        detail["macd_turning"] = round(10 * env_mult, 1)
        signals.append("MACD 柱翻红")
    else:
        detail["macd_turning"] = 0

    # ---- 6. 跌幅充分 ----
    pct10 = cur.get("pct_chg5", 0)
    decline_min = get_param(params, "decline_enough_min", -15.0)
    decline_max = get_param(params, "decline_enough_max", -5.0)

    if decline_min <= pct10 <= decline_max:
        detail["decline_enough"] = round(10 * env_mult, 1)
        signals.append(f"5日回调 {pct10:.1f}%（跌幅充分）")
    elif decline_max < pct10 <= -3:
        detail["decline_enough"] = round(7 * env_mult, 1)
    elif -20 < pct10 < decline_min:
        detail["decline_enough"] = round(5 * env_mult, 1)
    elif 0 < pct10 <= 2:
        detail["decline_enough"] = round(5 * env_mult, 1)
    else:
        detail["decline_enough"] = 0

    # ---- 7. 支撑位验证 (5) ----
    low = cur.get("low", 0)
    boll_lower = cur.get("boll_lower", 0)
    low_20 = cur.get("low_20", 0)
    score_support = 0.0
    if boll_lower > 0 and low <= boll_lower * 1.02:
        score_support += 3
        signals.append("触及布林下轨支撑")
    if low_20 > 0 and low <= low_20 * 1.01:
        score_support += 2
    detail["support_test"] = round(score_support * env_mult, 1)

    # ---- 特殊排除：还在加速下跌 ----
    accel_penalty = get_param(params, "accelerate_down_penalty", -5.0)
    if pct_chg < accel_penalty:
        penalty = abs(pct_chg) * 1.5
        for k in detail:
            detail[k] = max(0, detail[k] - penalty / len(detail))

    total = sum(detail.values())
    return {
        "score": round(min(total, 100), 1),
        "details": detail,
        "signals": signals,
    }


# ==========================================================================
# S3 — 动量突破策略
# ==========================================================================

def score_s3_breakout(df: pd.DataFrame, env: MarketEnv) -> dict:
    """S3：动量突破。

    捕捉放量突破关键阻力位的加速信号。8 因子，满分 100。

    环境调整：震荡市降低权重（突破策略在趋势市最有效）。
    所有参数从 strategy_params 表读取，支持数据驱动优化。
    """
    params = load_strategy_params("s3")
    cur = _latest(df)
    detail: dict[str, float] = {}
    signals: list[str] = []

    env_nontrend_mult = get_param(params, "env_nontrending_mult", 0.7)
    env_mult = env_nontrend_mult if not env.trending else 1.0

    close = cur.get("close", 0)

    # ---- 1. 突破强度 ----
    high_20 = cur.get("high_20", 0)
    high_10 = cur.get("high_10", 0)
    boll_upper = cur.get("boll_upper", 0)
    breakout_scores = get_param(params, "breakout_scores", [20, 15, 12, 6, 0])

    if high_20 > 0 and close >= high_20:
        detail["breakout_strength"] = round(breakout_scores[0] * env_mult, 1)
        signals.append("突破20日新高")
    elif high_10 > 0 and close >= high_10:
        detail["breakout_strength"] = round(breakout_scores[1] * env_mult, 1)
        signals.append("突破10日新高")
    elif boll_upper > 0 and close >= boll_upper:
        detail["breakout_strength"] = round(breakout_scores[2] * env_mult, 1)
        signals.append("突破布林上轨")
    elif boll_upper > 0 and close >= boll_upper * 0.98:
        detail["breakout_strength"] = round(breakout_scores[3] * env_mult, 1)
    else:
        detail["breakout_strength"] = breakout_scores[4] if len(breakout_scores) > 4 else 0

    # ---- 2. 放量确认 ----
    vol_ratio = cur.get("vol_ratio5", 1)
    vol_thresholds = get_param(params, "breakout_vol_thresholds", [2.0, 1.8, 1.5, 1.2, 1.0])
    vol_scores = get_param(params, "breakout_vol_scores", [18, 14, 10, 6, 3, 0])

    if not np.isnan(vol_ratio):
        scored = False
        for i, threshold in enumerate(vol_thresholds):
            if vol_ratio >= threshold:
                detail["volume_surge"] = round(vol_scores[i] * env_mult, 1)
                if i == 0:
                    signals.append(f"放量突破（量比{vol_ratio:.1f}）")
                scored = True
                break
        if not scored:
            detail["volume_surge"] = round(vol_scores[len(vol_thresholds)] * env_mult, 1) if len(vol_scores) > len(vol_thresholds) else 0
    else:
        detail["volume_surge"] = 0

    # ---- 3. 金叉信号 (15) ----
    ma_cross_5_20 = cur.get("ma_cross_5_20", 0)
    ma_cross_10_20 = cur.get("ma_cross_10_20", 0)
    ma5 = cur.get("ma5", 0)
    ma10 = cur.get("ma10", 0)

    if ma_cross_5_20 == 1 and ma10 > _prev(df).get("ma10", 0):
        detail["golden_cross"] = round(15 * env_mult, 1)
        signals.append("MA5 上穿 MA20（金叉）")
    elif ma_cross_5_20 == 1:
        detail["golden_cross"] = round(12 * env_mult, 1)
    elif ma_cross_10_20 == 1:
        detail["golden_cross"] = round(10 * env_mult, 1)
    elif ma5 > ma10:
        detail["golden_cross"] = round(5 * env_mult, 1)
    else:
        detail["golden_cross"] = 0

    # ---- 4. RSI 动能 ----
    rsi = cur.get("rsi14", 50)
    rm_center = get_param(params, "rsi_momentum_center", 63.0)
    rm_sigma = get_param(params, "rsi_momentum_sigma", 6.0)
    if not np.isnan(rsi):
        if 55 <= rsi <= 72:
            detail["rsi_momentum"] = round(_gaussian_score(rsi, rm_center, rm_sigma, 12) * env_mult, 1)
            signals.append(f"RSI={rsi:.0f} 强势但不极端")
        elif 50 <= rsi < 55:
            detail["rsi_momentum"] = round(8 * env_mult, 1)
        elif 72 < rsi <= 80:
            detail["rsi_momentum"] = round(5 * env_mult, 1)
        elif rsi > 80:
            detail["rsi_momentum"] = 0
        else:
            detail["rsi_momentum"] = 0
    else:
        detail["rsi_momentum"] = 0

    # ---- 5. MACD 金叉 (10) ----
    macd_cross = cur.get("macd_cross", 0)
    macd_hist = cur.get("macd_hist", 0)
    if macd_cross == 1 and macd_hist > 0:
        detail["macd_cross"] = round(10 * env_mult, 1)
        signals.append("MACD 金叉 + 红柱")
    elif macd_cross == 1:
        detail["macd_cross"] = round(7 * env_mult, 1)
    elif macd_hist > 0 and _prev(df).get("macd_hist", 0) <= 0:
        detail["macd_cross"] = round(5 * env_mult, 1)
    else:
        detail["macd_cross"] = 0

    # ---- 6. 蓄势质量 ----
    avg_amplitude = df["amplitude"].iloc[-6:-1].mean() if len(df) >= 6 else 10
    bq_max_amp = get_param(params, "base_quality_max_amp", 8.0)
    bq_scores = get_param(params, "base_quality_scores", [10, 7, 4, 0])

    if not np.isnan(avg_amplitude):
        if avg_amplitude < bq_max_amp:
            detail["base_quality"] = round(bq_scores[0] * env_mult, 1)
            signals.append("突破前横盘蓄势充分")
        elif avg_amplitude < bq_max_amp * 1.5:
            detail["base_quality"] = round(bq_scores[1] * env_mult, 1)
        elif avg_amplitude < bq_max_amp * 1.9:
            detail["base_quality"] = round(bq_scores[2] * env_mult, 1)
        else:
            detail["base_quality"] = bq_scores[3] if len(bq_scores) > 3 else 0
    else:
        detail["base_quality"] = 0

    # ---- 7. 布林带扩张 (10) ----
    boll_width = cur.get("boll_width", 0)
    prev_boll_width = _prev(df).get("boll_width", 0)
    pct_b = cur.get("boll_pct_b", 0.5)
    score_boll = 0.0
    if prev_boll_width > 0 and boll_width > prev_boll_width * 1.05:
        score_boll += 5
        signals.append("布林带扩张（波动率放大）")
    if pct_b > 0.9:
        score_boll += 5
    elif pct_b > 0.8:
        score_boll += 3
    detail["boll_expansion"] = round(score_boll * env_mult, 1)

    # ---- 8. 板块加分 (5) ----
    detail["sector_bonus"] = 0

    # ---- 特殊排除：追高风险 ----
    chase_penalty = get_param(params, "chase_high_penalty", 12.0)
    pct5 = cur.get("pct_chg5", 0)
    if pct5 > chase_penalty:
        penalty = (pct5 - chase_penalty) * 2
        for k in detail:
            detail[k] = max(0, detail[k] - penalty / len(detail))
        signals.append("⚠️ 短期涨幅过大，追高需谨慎")

    # 冲高回落检查
    upper_shadow = cur.get("upper_shadow", 0)
    open_price = cur.get("open", 0)
    shadow_penalty = get_param(params, "upper_shadow_penalty", 30.0)
    if open_price > 0 and close < open_price and not np.isnan(upper_shadow) and upper_shadow > shadow_penalty:
        penalty2 = upper_shadow * 0.3
        for k in detail:
            detail[k] = max(0, detail[k] - penalty2 / len(detail))
        signals.append("⚠️ 突破日冲高回落")

    total = sum(detail.values())
    return {
        "score": round(min(total, 100), 1),
        "details": detail,
        "signals": signals,
    }


# ==========================================================================
# 策略注册表
# ==========================================================================

STRATEGIES: dict[str, tuple[str, Callable]] = {
    "s1": ("趋势跟随", score_s1_trend),
    "s2": ("底部反转", score_s2_reversal),
    "s3": ("动量突破", score_s3_breakout),
}


# ==========================================================================
# S1 排除条件
# ==========================================================================

def _exclude_s1(df: pd.DataFrame) -> str | None:
    """S1 专属排除条件。"""
    cur = _latest(df)
    pct20 = cur.get("pct_chg20", 0)
    if pct20 > 25:
        return f"20日涨幅过大 ({pct20:.1f}%)，追高风险"
    avg_amount = df["amount"].iloc[-20:].mean()
    if avg_amount < 30_000_000:
        return f"日均成交额过低 ({avg_amount/10000:.0f}万)"
    return None


# ==========================================================================
# S2 排除条件
# ==========================================================================

def _exclude_s2(df: pd.DataFrame) -> str | None:
    """S2 专属排除条件。"""
    cur = _latest(df)
    pct20 = cur.get("pct_chg20", 0)
    if pct20 < -25:
        return f"20日跌幅过大 ({pct20:.1f}%)，倾泻式下跌"
    pct_chg = cur.get("pct_chg", 0)
    if pct_chg < -5:
        return f"当日仍在加速下跌 ({pct_chg:.1f}%)"
    avg_amount = df["amount"].iloc[-20:].mean()
    if avg_amount < 20_000_000:
        return f"日均成交额过低 ({avg_amount/10000:.0f}万)"
    return None


# ==========================================================================
# S3 排除条件
# ==========================================================================

def _exclude_s3(df: pd.DataFrame) -> str | None:
    """S3 专属排除条件。"""
    cur = _latest(df)
    pct5 = cur.get("pct_chg5", 0)
    if pct5 > 12:
        return f"突破前5日涨幅已大 ({pct5:.1f}%)"
    avg_amount = df["amount"].iloc[-20:].mean()
    if avg_amount < 40_000_000:
        return f"日均成交额过低 ({avg_amount/10000:.0f}万)"
    # 冲高回落
    upper_shadow = cur.get("upper_shadow", 0)
    open_price = cur.get("open", 0)
    close = cur.get("close", 0)
    if open_price > 0 and close < open_price and not np.isnan(upper_shadow) and upper_shadow > 50:
        return "突破日严重冲高回落"
    return None


EXCLUSIONS: dict[str, Callable] = {
    "s1": _exclude_s1,
    "s2": _exclude_s2,
    "s3": _exclude_s3,
}


# ==========================================================================
# 筛选引擎
# ==========================================================================

def screen_stock(
    df: pd.DataFrame,
    code: str,
    name: str,
    strategy: str,
    env: MarketEnv,
) -> ScreenResult | None:
    """对单只股票执行策略筛选。

    Args:
        df: 已含技术指标的日线 DataFrame
        code: 股票代码
        name: 股票名称
        strategy: s1/s2/s3
        env: 市场环境

    Returns:
        ScreenResult 如果通过筛选，否则 None
    """
    # 通用排除
    exclusion = _apply_exclusions(df, code, name)
    if exclusion:
        return None

    # 策略专属排除
    strategy_exclusion_fn = EXCLUSIONS.get(strategy)
    if strategy_exclusion_fn:
        exclusion = strategy_exclusion_fn(df)
        if exclusion:
            return None

    # 执行策略打分
    strategy_name, score_func = STRATEGIES[strategy]
    result = score_func(df, env)

    if result["score"] <= 0:
        return None

    # 提取关键指标
    cur = _latest(df)
    metrics = {
        "close": float(cur.get("close", 0)),
        "pct_chg5": float(cur.get("pct_chg5", 0)),
        "pct_chg20": float(cur.get("pct_chg20", 0)),
        "vol_ratio5": float(cur.get("vol_ratio5", 0)),
        "rsi14": float(cur.get("rsi14", 50)),
        "ma_bull_score": float(cur.get("ma_bull_score", 0)),
        "boll_pct_b": float(cur.get("boll_pct_b", 0.5)),
    }

    return ScreenResult(
        code=code,
        name=name,
        strategy=strategy,
        strategy_name=strategy_name,
        score=result["score"],
        factors_detail=result["details"],
        signals=result["signals"],
        metrics={k: round(v, 3) if not np.isnan(v) else 0 for k, v in metrics.items()},
    )
