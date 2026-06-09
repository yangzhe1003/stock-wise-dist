"""精筛引擎 — 元打分系统。

在三策略之上构建 5 维度加权评分模型，从历史盈亏数据中持续学习：
- 策略得分：S1/S2/S3 各自得分，按策略历史胜率加权
- 策略共识：同日被多策略同时选中
- 因子信号：各策略内具体因子的触发
- 多日持续性：同一股票连续多日出现在策略结果中
- 市场适配：当前市场环境与策略的匹配度

进化机制：
- learn_weights(): Bayesian 更新各信号权重
- learn_strategy_params(): 网格搜索优化策略参数
- analyze_factor_effectiveness(): 分析各因子预测能力
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from app.core.sqlite_storage import get_storage
from app.services.strategy_engine import (
    STRATEGIES,
    MarketEnv,
    clear_params_cache,
    load_strategy_params,
)

# ==========================================================================
# 数据模型
# ==========================================================================


@dataclass
class PrecisionCandidate:
    """精筛候选股。"""
    code: str
    name: str
    precision_score: float            # 综合精筛得分 0-100
    rank: int
    pick_price: float
    dimension_scores: dict[str, float]  # 各维度得分
    reasons: list[dict]               # [{type, desc, value}]
    feature_scores: dict[str, float]    # 所有子信号得分


# ==========================================================================
# 5 维度子信号定义
# ==========================================================================

# 各策略因子的信号名映射
FACTOR_SIGNAL_MAP = {
    "s1": ["ma_alignment", "trend_health", "pullback_support", "macd_strength",
           "vol_healthy", "boll_direction", "relative_strength"],
    "s2": ["rsi_divergence", "oversold_level", "volume_drying", "price_stabilizing",
           "macd_turning", "decline_enough", "support_test"],
    "s3": ["breakout_strength", "volume_surge", "golden_cross", "rsi_momentum",
           "macd_cross", "base_quality", "boll_expansion"],
}

# 维度分类 → 信号前缀（用于 signal_weights 表匹配）
DIMENSION_WEIGHT_CATEGORIES = {
    "strategy_score": "strategy_score",
    "consensus": "consensus",
    "factor_detail": "factor_detail",
    "persistence": "persistence",
    "market_env": "market_env",
}


# ==========================================================================
# 精筛引擎
# ==========================================================================


class PrecisionEngine:
    """精筛元打分引擎。"""

    def __init__(self, storage=None):
        self.storage = storage or get_storage()

    # ------------------------------------------------------------------
    # 主入口：生成精选
    # ------------------------------------------------------------------

    def generate_picks(self, trade_date: str, top_n: int = 5) -> list[dict[str, Any]]:
        """生成当日精筛。

        1. 收集候选池
        2. 加载信号权重
        3. 对每只候选股计算 5 维度得分
        4. 加权求和排名
        5. 持久化
        """
        # ---- 收集候选池 ----
        candidates = self._collect_candidates(trade_date)
        if not candidates:
            return []

        # ---- 加载权重 ----
        weights_map = self.storage.get_signal_weights_map()
        dim_weights = self._get_dimension_weights(weights_map)

        # ---- 加载市场环境 ----
        env = self._get_market_env()

        # ---- 对每只候选股打分 ----
        scored: list[PrecisionCandidate] = []
        for c in candidates.values():
            result = self._score_candidate(c, env, weights_map, dim_weights)
            if result is not None:
                scored.append(result)

        # ---- 按精筛得分排序 ----
        scored.sort(key=lambda x: x.precision_score, reverse=True)
        for i, s in enumerate(scored):
            s.rank = i + 1

        top_picks = scored[:top_n]

        # ---- 持久化 ----
        picks_data = []
        for p in top_picks:
            picks_data.append({
                "code": p.code,
                "name": p.name,
                "pick_price": p.pick_price,
                "precision_score": round(p.precision_score, 1),
                "rank": p.rank,
                "reasons": p.reasons,
                "feature_scores": p.feature_scores,
                "signal_weights": {k: v.get("weight", 1.0) for k, v in weights_map.items()},
            })

        self.storage.save_precision_picks(trade_date, picks_data)

        # ---- 记录日志 ----
        params_snapshot = {}
        for s in ["s1", "s2", "s3"]:
            params_snapshot[s] = load_strategy_params(s)

        self.storage.save_precision_log(
            trade_date=trade_date,
            total_candidates=len(candidates),
            picks_count=len(top_picks),
            weights_snapshot={k: v.get("weight", 1.0) for k, v in weights_map.items()},
            params_snapshot=params_snapshot,
        )

        return picks_data

    # ------------------------------------------------------------------
    # 候选池收集
    # ------------------------------------------------------------------

    def _collect_candidates(self, trade_date: str) -> dict[str, dict]:
        """收集候选池：今日所有策略结果 + 过去3日持续出现的股。"""
        candidates: dict[str, dict] = {}

        # 当日策略结果
        all_results = self.storage.get_all_strategy_results_for_date(trade_date)

        for strategy, results in all_results.items():
            for r in results:
                code = r["code"]
                if code not in candidates:
                    candidates[code] = {
                        "code": code,
                        "name": r["name"],
                        "strategy_scores": {},
                        "factor_details": {},
                        "signals": {},
                        "metrics": {},
                    }
                c = candidates[code]
                c["strategy_scores"][strategy] = r["score"]
                c["factor_details"][strategy] = r.get("factors_detail", {})
                c["signals"][strategy] = r.get("signals", [])
                c["metrics"][strategy] = r.get("metrics", {})

        # 共识股补充（跨策略出现的股票）
        consensus = self.storage.get_consensus_stocks("", min_strategies=2)  # scan_id 不需要，会从 trade_date 查
        for con in consensus:
            code = con["code"]
            if code not in candidates:
                # 共识股可能在个别策略中分数不高，但仍纳入候选池
                candidates[code] = {
                    "code": code,
                    "name": con.get("name", ""),
                    "strategy_scores": {},
                    "factor_details": {},
                    "signals": {},
                    "metrics": {},
                    "consensus_strategies": con.get("strategies", []),
                    "consensus_avg_score": con.get("avg_score", 0),
                }
            else:
                candidates[code]["consensus_strategies"] = con.get("strategies", [])
                candidates[code]["consensus_avg_score"] = con.get("avg_score", 0)

        return candidates

    # ------------------------------------------------------------------
    # 维度权重
    # ------------------------------------------------------------------

    def _get_dimension_weights(self, weights_map: dict) -> dict[str, float]:
        """从 signal_weights 中提取 5 大维度的权重（归一化）。"""
        raw = {
            "strategy_score": 0.30,
            "consensus": 0.25,
            "factor_detail": 0.20,
            "persistence": 0.15,
            "market_env": 0.10,
        }
        # 如果数据库中已有学习过的权重，用学习值覆盖初始值
        # 维度权重 = 该维度下所有信号权重的均值
        dim_sums: dict[str, float] = {k: 0.0 for k in raw}
        dim_counts: dict[str, int] = {k: 0 for k in raw}
        for name, info in weights_map.items():
            cat = info.get("category", "")
            if cat in dim_sums:
                dim_sums[cat] += info.get("weight", 1.0)
                dim_counts[cat] += 1

        learned: dict[str, float] = {}
        for k in raw:
            if dim_counts[k] > 0:
                learned[k] = dim_sums[k] / dim_counts[k]
            else:
                learned[k] = raw[k]

        # 归一化
        total = sum(learned.values())
        if total > 0:
            learned = {k: v / total for k, v in learned.items()}
        return learned

    # ------------------------------------------------------------------
    # 市场环境
    # ------------------------------------------------------------------

    def _get_market_env(self) -> MarketEnv:
        """获取当前市场环境。"""
        overview_data = self.storage.get_market_overview()
        if overview_data:
            return MarketEnv.from_overview(overview_data)
        return MarketEnv(
            breadth_ratio=0.5, index_change_pct=0,
            phase="morning", trending=False, bullish=False, bearish=False,
        )

    # ------------------------------------------------------------------
    # 单只候选股打分
    # ------------------------------------------------------------------

    def _score_candidate(
        self, candidate: dict, env: MarketEnv,
        weights_map: dict, dim_weights: dict,
    ) -> PrecisionCandidate | None:
        """对单只候选股计算 5 维度精筛得分。"""
        dim_scores: dict[str, float] = {}
        reasons: list[dict] = []
        feature_scores: dict[str, float] = {}

        # ---- 1. 策略得分维度 (30%) ----
        ss_score, ss_reasons, ss_features = self._score_strategy_scores(candidate, weights_map)
        dim_scores["strategy_score"] = ss_score
        reasons.extend(ss_reasons)
        feature_scores.update(ss_features)

        # ---- 2. 策略共识维度 (25%) ----
        cs_score, cs_reasons, cs_features = self._score_consensus(candidate, weights_map)
        dim_scores["consensus"] = cs_score
        reasons.extend(cs_reasons)
        feature_scores.update(cs_features)

        # ---- 3. 因子信号维度 (20%) ----
        fd_score, fd_reasons, fd_features = self._score_factor_details(candidate, weights_map)
        dim_scores["factor_detail"] = fd_score
        reasons.extend(fd_reasons)
        feature_scores.update(fd_features)

        # ---- 4. 多日持续性维度 (15%) ----
        ps_score, ps_reasons, ps_features = self._score_persistence(candidate, weights_map)
        dim_scores["persistence"] = ps_score
        reasons.extend(ps_reasons)
        feature_scores.update(ps_features)

        # ---- 5. 市场适配维度 (10%) ----
        me_score, me_reasons, me_features = self._score_market_fit(candidate, env, weights_map)
        dim_scores["market_env"] = me_score
        reasons.extend(me_reasons)
        feature_scores.update(me_features)

        # ---- 加权求和 ----
        precision = sum(
            dim_scores.get(dim, 0) * dim_weights.get(dim, 0)
            for dim in dim_weights
        )

        # 归一化到 0-100
        precision = min(precision * 100, 100)

        if precision <= 0:
            return None

        # 取入选价
        pick_price = 0.0
        for s in ["s1", "s2", "s3"]:
            metrics = candidate.get("metrics", {}).get(s, {})
            if metrics.get("close", 0) > 0:
                pick_price = metrics["close"]
                break
        if pick_price <= 0:
            return None

        return PrecisionCandidate(
            code=candidate["code"],
            name=candidate["name"],
            precision_score=precision,
            rank=0,
            pick_price=pick_price,
            dimension_scores=dim_scores,
            reasons=reasons[:10],  # Top 10 reasons
            feature_scores=feature_scores,
        )

    # ==================================================================
    # 维度 1: 策略得分
    # ==================================================================

    def _score_strategy_scores(self, candidate: dict, weights_map: dict) -> tuple[float, list, dict]:
        """按策略历史胜率加权合并 S1/S2/S3 得分。"""
        reasons: list[dict] = []
        features: dict[str, float] = {}
        weighted_sum = 0.0
        total_weight = 0.0

        strategy_scores = candidate.get("strategy_scores", {})
        for s_key in ["s1", "s2", "s3"]:
            score = strategy_scores.get(s_key, 0)
            if score <= 0:
                continue
            # 获取该策略的当前权重（从 signal_weights 中 s1_score/s2_score/s3_score）
            w_info = weights_map.get(f"{s_key}_score", {})
            w = w_info.get("weight", 1.0)
            features[f"{s_key}_score"] = score
            weighted_sum += score * w
            total_weight += w
            if score >= 70:
                reasons.append({
                    "type": "strategy_score",
                    "desc": f"{STRATEGIES[s_key][0]}得分 {score:.0f}",
                    "value": score,
                })

        if total_weight == 0:
            return 0.0, reasons, features

        normalized = weighted_sum / total_weight / 100.0  # 归一化到 0-1
        return min(normalized, 1.0), reasons, features

    # ==================================================================
    # 维度 2: 策略共识
    # ==================================================================

    def _score_consensus(self, candidate: dict, weights_map: dict) -> tuple[float, list, dict]:
        """计算跨策略共识得分。"""
        reasons: list[dict] = []
        features: dict[str, float] = {}

        strategies = candidate.get("consensus_strategies", [])
        strategy_count = len(strategies)
        strategy_scores = candidate.get("strategy_scores", {})

        # 实际出现在多少个策略中
        actual_count = sum(1 for s in ["s1", "s2", "s3"] if s in strategy_scores and strategy_scores[s] > 0)
        if actual_count == 0 and strategy_count == 0:
            return 0.0, reasons, features

        count = max(actual_count, strategy_count)

        # 2 策略共识得分
        w2 = weights_map.get("consensus_2strategy", {}).get("weight", 1.0)
        # 3 策略共识得分
        w3 = weights_map.get("consensus_3strategy", {}).get("weight", 1.0)
        # 平均得分权重
        w_avg = weights_map.get("consensus_avg_score", {}).get("weight", 1.0)

        if count >= 3:
            features["consensus_3strategy"] = 1.0
            reasons.append({"type": "consensus", "desc": "三策略同时选中（强共识）", "value": 3})
        elif count >= 2:
            features["consensus_2strategy"] = 1.0
            reasons.append({"type": "consensus", "desc": "双策略共振", "value": 2})

        # 共识平均分
        if strategy_scores:
            avg = sum(strategy_scores.values()) / len(strategy_scores)
            features["consensus_avg_score"] = avg
            if avg >= 60:
                reasons.append({"type": "consensus", "desc": f"跨策略均分 {avg:.0f}", "value": avg})

        score_2 = (1.0 if count >= 2 else 0.0) * w2
        score_3 = (1.0 if count >= 3 else 0.0) * w3
        score_avg = min(features.get("consensus_avg_score", 0) / 100.0, 1.0) * w_avg

        total_w = w2 + w3 + w_avg
        if total_w == 0:
            return 0.0, reasons, features

        return min((score_2 + score_3 + score_avg) / total_w, 1.0), reasons, features

    # ==================================================================
    # 维度 3: 因子信号
    # ==================================================================

    def _score_factor_details(self, candidate: dict, weights_map: dict) -> tuple[float, list, dict]:
        """对具体因子的触发打分。每个策略的因子单独评估。"""
        reasons: list[dict] = []
        features: dict[str, float] = {}
        weighted_sum = 0.0
        total_weight = 0.0

        factor_details = candidate.get("factor_details", {})

        for s_key, factor_names in FACTOR_SIGNAL_MAP.items():
            details = factor_details.get(s_key, {})
            if not details:
                continue
            for f_name in factor_names:
                value = details.get(f_name, 0)
                if value <= 0:
                    continue
                signal_name = f"{s_key}_{f_name}"
                w_info = weights_map.get(signal_name, {})
                w = w_info.get("weight", 1.0)

                features[signal_name] = value
                weighted_sum += (value / 100.0) * w
                total_weight += w

                # 高分因子记录为理由
                if value >= 80:
                    reasons.append({
                        "type": "factor_detail",
                        "desc": f"{STRATEGIES[s_key][0]}·{f_name}",
                        "value": value,
                    })

        if total_weight == 0:
            return 0.0, reasons, features

        return min(weighted_sum / total_weight, 1.0), reasons, features

    # ==================================================================
    # 维度 4: 多日持续性
    # ==================================================================

    def _score_persistence(self, candidate: dict, weights_map: dict) -> tuple[float, list, dict]:
        """检查股票是否连续多日出现在策略结果中。"""
        reasons: list[dict] = []
        features: dict[str, float] = {}
        code = candidate["code"]

        # 查询最近 3 个交易日的策略结果
        recent_dates = self._get_recent_trade_dates(3)
        if len(recent_dates) < 2:
            return 0.0, reasons, features

        appearances = 0
        scores_on_days: list[float] = []
        for d in recent_dates:
            all_results = self.storage.get_all_strategy_results_for_date(d)
            for s, results in all_results.items():
                for r in results:
                    if r["code"] == code:
                        appearances += 1
                        scores_on_days.append(r.get("score", 0))
                        break  # 同一天同一策略只算一次

        if appearances < 2:
            return 0.0, reasons, features

        w2 = weights_map.get("persistence_2day", {}).get("weight", 1.0)
        w3 = weights_map.get("persistence_3day", {}).get("weight", 1.0)
        ws = weights_map.get("persistence_streak_score", {}).get("weight", 1.0)

        # 按连续天数
        features["persistence_2day"] = 1.0 if appearances >= 2 else 0.0
        features["persistence_3day"] = 1.0 if appearances >= 3 else 0.0

        # 平均得分
        avg_streak_score = sum(scores_on_days) / len(scores_on_days) if scores_on_days else 0
        features["persistence_streak_score"] = avg_streak_score

        if appearances >= 3:
            reasons.append({"type": "persistence", "desc": f"连续 3 天出现在策略 Top 中", "value": appearances})
        elif appearances >= 2:
            reasons.append({"type": "persistence", "desc": f"连续 2 天出现", "value": appearances})

        s2 = (1.0 if appearances >= 2 else 0.0) * w2
        s3 = (1.0 if appearances >= 3 else 0.0) * w3
        s_avg = min(avg_streak_score / 100.0, 1.0) * ws

        total_w = w2 + w3 + ws
        if total_w == 0:
            return 0.0, reasons, features

        return min((s2 + s3 + s_avg) / total_w, 1.0), reasons, features

    def _get_recent_trade_dates(self, n: int) -> list[str]:
        """获取最近 N 个有策略结果的交易日。"""
        dates_set: set[str] = set()
        for s in ["s1", "s2", "s3"]:
            ds = self.storage.get_strategy_dates(s, n + 1)
            dates_set.update(ds)
        return sorted(dates_set, reverse=True)[:n + 1]

    # ==================================================================
    # 维度 5: 市场适配
    # ==================================================================

    def _score_market_fit(self, candidate: dict, env: MarketEnv,
                          weights_map: dict) -> tuple[float, list, dict]:
        """评估股票与当前市场环境的适配度。"""
        reasons: list[dict] = []
        features: dict[str, float] = {}

        # S1 适配：趋势市场
        s1_fit = 0.0
        if env.trending and env.bullish:
            s1_fit = 1.0
        elif env.trending:
            s1_fit = 0.6
        elif env.bullish:
            s1_fit = 0.4

        # S2 适配：熊市/超卖环境
        s2_fit = 0.0
        if env.bearish:
            s2_fit = 1.0
        elif env.breadth_ratio < 0.4:
            s2_fit = 0.7
        elif env.breadth_ratio < 0.45:
            s2_fit = 0.4

        # S3 适配：趋势市场
        s3_fit = 0.0
        if env.trending:
            s3_fit = env.breadth_ratio if env.bullish else 1.0 - env.breadth_ratio

        features["market_s1_fit"] = s1_fit
        features["market_s2_fit"] = s2_fit
        features["market_s3_fit"] = s3_fit

        # 市场阶段得分
        phase_score = 0.5  # 默认中性
        if env.phase == "morning":
            phase_score = 0.5
        features["market_phase_score"] = phase_score

        w1 = weights_map.get("market_s1_fit", {}).get("weight", 1.0)
        w2 = weights_map.get("market_s2_fit", {}).get("weight", 1.0)
        w3 = weights_map.get("market_s3_fit", {}).get("weight", 1.0)
        wp = weights_map.get("market_phase_score", {}).get("weight", 1.0)

        # 检查该股出现在哪些策略中
        strategy_scores = candidate.get("strategy_scores", {})
        applicable_fit = 0.0
        applicable_w = 0.0
        for s_key, w_key, fit_val in [("s1", "market_s1_fit", s1_fit), ("s2", "market_s2_fit", s2_fit), ("s3", "market_s3_fit", s3_fit)]:
            if strategy_scores.get(s_key, 0) > 0:
                w = weights_map.get(w_key, {}).get("weight", 1.0)
                applicable_fit += fit_val * w
                applicable_w += w

        if env.trending and env.bullish:
            reasons.append({"type": "market_env", "desc": "趋势牛市，顺势策略占优", "value": 0.8})
        elif env.bearish:
            reasons.append({"type": "market_env", "desc": "偏空环境，反转策略占优", "value": 0.7})

        total_w = w1 + w2 + w3 + wp
        if total_w == 0:
            return 0.0, reasons, features

        # 综合
        raw = (applicable_fit + phase_score * wp) / (applicable_w + wp) if (applicable_w + wp) > 0 else 0
        return min(raw, 1.0), reasons, features

    # ==================================================================
    # Outcome 判定
    # ==================================================================

    def update_outcomes(self):
        """更新所有 pending 精筛股的 latest_price + return_pct，并自动判定到期 outcome。"""
        self.storage.update_precision_outcomes()
        self.storage.auto_judge_outcomes()

    # ==================================================================
    # 权重学习
    # ==================================================================

    def learn_weights(self, min_samples: int = 30):
        """从历史盈亏数据学习各信号权重（Bayesian 更新）。

        对每个信号 s：
        - 从 precision_picks + strategy_results 提取信号值与后续收益
        - 计算 IC (Pearson 相关系数) 和 win_rate
        - w_new = w_old * 0.7 + IC * win_rate * 0.3
        - 如果 IC < 0，权重设为 0.01（标记为反向信号）
        - 维度内归一化
        """
        weights_list = self.storage.get_signal_weights()
        if not weights_list:
            return {"error": "无信号权重数据，请先初始化"}

        # 收集所有已判定 outcome 的精筛记录
        judged_picks = self._get_judged_picks()
        if len(judged_picks) < min_samples:
            return {"error": f"已判定样本不足，当前 {len(judged_picks)}，需要 {min_samples}"}

        # 获取每只精筛股对应日期的策略因子明细
        # 构建 signal → [return_pcts] 映射
        signal_returns: dict[str, list[float]] = {}
        for pick in judged_picks:
            ret = pick.get("return_pct")
            if ret is None:
                continue
            trade_date = pick["trade_date"]
            code = pick["code"]

            # 获取该股在该日的策略得分和因子明细
            factor_data = self._get_stock_factor_data(code, trade_date)
            if not factor_data:
                continue

            # 策略得分信号
            for s in ["s1", "s2", "s3"]:
                score = factor_data.get("strategy_scores", {}).get(s, 0)
                if score > 0:
                    signal_returns.setdefault(f"{s}_score", []).append(ret)

            # 因子明细信号
            for s, factors in factor_data.get("factor_details", {}).items():
                for f_name, f_value in factors.items():
                    if f_value > 0:
                        signal_returns.setdefault(f"{s}_{f_name}", []).append(ret)

            # 共识信号
            if factor_data.get("consensus_count", 0) >= 2:
                signal_returns.setdefault("consensus_2strategy", []).append(ret)
            if factor_data.get("consensus_count", 0) >= 3:
                signal_returns.setdefault("consensus_3strategy", []).append(ret)

        # 更新每个信号的权重
        updates = []
        for sw in weights_list:
            name = sw["signal_name"]
            rets = signal_returns.get(name, [])
            n = len(rets)

            if n < 5:
                # 样本太少，保持原权重
                self.storage.update_signal_samples(
                    name, n, sum(1 for r in rets if r > 0),
                    sum(1 for r in rets if r > 0) / max(n, 1) if n > 0 else 0,
                    np.mean(rets) if n > 0 else 0,
                )
                continue

            win_rate = sum(1 for r in rets if r > 0) / n
            avg_ret = np.mean(rets)

            # 计算 IC：信号触发强度 vs 收益（简化版，用触发=1/0 的 point-biserial）
            # 如果没有信号值细度，用 win_rate 作为 IC 的代理
            ic = win_rate * 2 - 1  # 映射 win_rate 0-1 → IC -1到1

            # Bayesian 更新
            alpha = 0.7
            old_weight = sw["weight"]
            new_weight = old_weight * alpha + ic * win_rate * (1 - alpha)
            new_weight = max(0.01, min(new_weight, 5.0))  # 裁剪到 [0.01, 5.0]

            if ic < -0.1:
                new_weight = 0.01  # 负相关信号权重降到最低

            self.storage.update_signal_weight(name, new_weight, n, win_rate, avg_ret, ic)
            updates.append({
                "signal": name,
                "old_weight": round(old_weight, 3),
                "new_weight": round(new_weight, 3),
                "samples": n,
                "win_rate": round(win_rate * 100, 1),
                "avg_return": round(avg_ret, 2),
                "ic": round(ic, 3),
            })

        # 维度内归一化
        self._normalize_dimension_weights()

        return {"updated": len(updates), "details": updates[:20]}

    def _get_judged_picks(self) -> list[dict]:
        """获取所有已判定 outcome 的精筛记录。"""
        results, total = self.storage.get_precision_history(page=1, page_size=10000)
        return [r for r in results if r.get("outcome") != "pending" and r.get("return_pct") is not None]

    def _get_stock_factor_data(self, code: str, trade_date: str) -> dict | None:
        """获取某股票在指定交易日的策略因子数据。"""
        all_results = self.storage.get_all_strategy_results_for_date(trade_date)
        data = {
            "strategy_scores": {},
            "factor_details": {},
            "consensus_count": 0,
        }
        for s, results in all_results.items():
            for r in results:
                if r["code"] == code:
                    data["strategy_scores"][s] = r.get("score", 0)
                    data["factor_details"][s] = r.get("factors_detail", {})
                    data["consensus_count"] += 1
        return data if data["strategy_scores"] else None

    def _normalize_dimension_weights(self):
        """对各维度内的信号权重做归一化。"""
        weights = self.storage.get_signal_weights()
        dims: dict[str, list[str]] = {}
        for sw in weights:
            cat = sw["category"]
            dims.setdefault(cat, []).append(sw["signal_name"])

        for cat, names in dims.items():
            cat_weights = [sw for sw in weights if sw["signal_name"] in names]
            total = sum(sw["weight"] for sw in cat_weights)
            if total > 0:
                for sw in cat_weights:
                    normalized = sw["weight"] / total * len(cat_weights)  # 保持量级
                    self.storage.update_signal_weight(
                        sw["signal_name"], normalized,
                        sw["sample_count"], sw["win_rate"],
                        sw["avg_return"], sw["information_coef"],
                    )

    # ==================================================================
    # 因子有效性分析
    # ==================================================================

    def analyze_factor_effectiveness(self):
        """分析各策略各因子的历史预测能力，更新 factor_effectiveness 表。"""
        # 获取所有已判定 outcome 的精筛记录
        judged = self._get_judged_picks()
        if not judged:
            return {"error": "无已判定样本"}

        # 按策略 × 因子组织数据
        # strategy → factor_name → [return_pcts]
        factor_data: dict[str, dict[str, list[float]]] = {
            "s1": {}, "s2": {}, "s3": {},
        }

        for pick in judged:
            ret = pick.get("return_pct")
            if ret is None:
                continue
            trade_date = pick["trade_date"]
            code = pick["code"]
            stock_data = self._get_stock_factor_data(code, trade_date)
            if not stock_data:
                continue

            for s, factors in stock_data.get("factor_details", {}).items():
                for f_name, f_value in factors.items():
                    if f_value > 0:
                        factor_data.setdefault(s, {}).setdefault(f_name, []).append(ret)

        # 计算统计量并存储
        results = []
        for strategy in ["s1", "s2", "s3"]:
            for f_name, rets in factor_data.get(strategy, {}).items():
                n = len(rets)
                if n < 5:
                    continue
                pos = sum(1 for r in rets if r > 0)
                wr = pos / n
                avg_r = np.mean(rets)
                ic = wr * 2 - 1  # 简化 IC

                self.storage.update_factor_effectiveness(
                    strategy, f_name, n, pos, round(wr, 4),
                    round(avg_r, 4), round(ic, 4),
                )
                results.append({
                    "strategy": strategy, "factor": f_name,
                    "samples": n, "win_rate": round(wr * 100, 1),
                    "avg_return": round(avg_r, 2), "ic": round(ic, 3),
                })

        return {"analyzed_factors": len(results), "details": results}

    # ==================================================================
    # 策略参数优化 (网格搜索)
    # ==================================================================

    def learn_strategy_params(self, min_samples: int = 50):
        """对可学习参数做网格搜索，找到最大化 win_rate * avg_return 的参数值。

        只优化有明确 min/max/step 的数值型参数。
        """
        judged = self._get_judged_picks()
        if len(judged) < min_samples:
            return {"error": f"已判定样本不足，当前 {len(judged)}，需要 {min_samples}"}

        all_updates = []

        for strategy in ["s1", "s2", "s3"]:
            params = self.storage.get_strategy_params(strategy)
            for p in params:
                if p["param_type"] not in ("float", "int"):
                    continue
                if p["min_value"] is None or p["max_value"] is None or p["step"] is None:
                    continue
                if p["step"] <= 0:
                    continue

                param_name = p["param_name"]
                current = json.loads(p["current_value"]) if isinstance(p["current_value"], str) else p["current_value"]
                best_value = current
                best_score = -999.0

                # 网格搜索
                val = p["min_value"]
                while val <= p["max_value"]:
                    # 临时设置参数
                    self.storage.update_strategy_param(strategy, param_name, json.dumps(val), "grid_search")

                    # 评估：用历史数据回测此参数值
                    score = self._evaluate_param(strategy, param_name, val, judged)
                    if score > best_score:
                        best_score = score
                        best_value = val

                    val += p["step"]

                # 恢复当前值或更新
                if best_value != current and best_score > 0:
                    self.storage.update_strategy_param(strategy, param_name, json.dumps(best_value), "auto_optimize")
                    all_updates.append({
                        "strategy": strategy,
                        "param": param_name,
                        "old_value": current,
                        "new_value": best_value,
                        "score_improvement": round(best_score, 4),
                    })
                else:
                    # 恢复原值（清除临时设置）
                    self.storage.update_strategy_param(strategy, param_name, json.dumps(current), "grid_search_restore")

        # 清除策略参数缓存
        clear_params_cache()

        return {"optimized_params": len(all_updates), "details": all_updates}

    def _evaluate_param(self, strategy: str, param_name: str, value: float,
                        judged: list[dict]) -> float:
        """评估一个参数值的好坏。

        简化版评估：用参数变化率作为 proxy。
        实际使用时需要重新计算策略得分 → 这里用一个简化的启发式方法。
        """
        # 获取该参数对应因子的历史表现作为参照
        # 如果参数是中值型(center/sigma)，用 IC 方向评估
        # 如果是阈值型，用 win_rate 评估

        # 简化实现：检查 factor_effectiveness 中相关因子的 IC
        # 按 IC * avg_return 的方向调整参数
        effectiveness = self.storage.get_factor_effectiveness(strategy)

        # 尝试匹配参数名到因子名
        # 例如 trend_health_center → trend_health
        related_factor = None
        for f in effectiveness:
            f_name = f["factor_name"]
            if f_name in param_name or param_name.startswith(f_name):
                related_factor = f
                break

        if related_factor and related_factor["sample_count"] >= 10:
            ic = related_factor["ic"]
            # 如果 IC 为正，向中心移动可能提升效果
            return abs(ic) * related_factor["win_rate"]
        return 0.0
