"""策略选股 API 路由。

当天首次扫描时自动拉取全市场 K 线数据并缓存至 SQLite，
后续扫描直接复用缓存，显著加速。
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Query

from app.core.response import fail, ok
from app.core.sqlite_storage import get_storage
from app.services.market_calendar import get_market_status
from app.services.market_service import get_market_overview
from app.services.mootdx_client import client as mootdx_client
from app.services.stock_universe import get_universe
from app.services.strategy_engine import (
    STRATEGIES,
    MarketEnv,
    ScreenResult,
    screen_stock,
)
from app.services.technical_indicators import enrich_indicators

router = APIRouter(prefix="/strategy", tags=["strategy"])

# ---------------------------------------------------------------------------
# 扫描协调
# ---------------------------------------------------------------------------

_scan_lock = threading.Lock()
_data_lock = threading.Lock()
_current_scan_id: str | None = None


def is_scanning() -> bool:
    return _current_scan_id is not None


# ---------------------------------------------------------------------------
# 数据缓存层
# ---------------------------------------------------------------------------

def _ensure_kline_cache(trade_date: str):
    """确保当天全市场 K 线数据已缓存至 SQLite。

    仅当天首次调用时执行批量拉取，后续调用直接跳过。
    覆盖率检查不仅看行数，还验证最新日期是否覆盖 trade_date。
    """
    import pandas as pd

    storage = get_storage()
    print("[数据缓存] 正在加载股票池...")
    universe = get_universe()
    codes = [item["code"] for item in universe]
    total = len(codes)
    print(f"[数据缓存] 股票池加载完成: {total} 只")
    if total == 0:
        return

    # 检查缓存覆盖率：抽样 20 只，同时检查最新日期是否为 trade_date
    sample = codes[:20] if len(codes) > 20 else codes
    cached_count = 0
    start_dt = pd.to_datetime(trade_date) - pd.Timedelta(days=300)
    start_str = start_dt.strftime("%Y%m%d")
    for code in sample:
        if storage.has_kline_data(code, start_str, trade_date, min_rows=60):
            # 额外检查：最新日期必须覆盖 trade_date，否则视为过期
            latest = storage.max_kline_date(code)
            if latest and latest >= trade_date:
                cached_count += 1

    coverage = cached_count / len(sample) if sample else 0
    print(f"[数据缓存] 抽样 {len(sample)} 只，覆盖率 {coverage:.0%}（含日期校验）")

    if coverage >= 0.8:
        print("[数据缓存] 覆盖率充足，跳过全量拉取")
        return

    # 覆盖率不足，批量拉取
    print(f"[数据缓存] 开始拉取全市场 K 线 ({total} 只股票)...")
    quote_client = mootdx_client.get_client()
    fetched = 0
    start_time = time.time()

    for i, code in enumerate(codes):
        try:
            # 跳过已缓存且日期为今天的
            if storage.has_kline_data(code, start_str, trade_date, min_rows=60):
                latest = storage.max_kline_date(code)
                if latest and latest >= trade_date:
                    continue

            raw = quote_client.bars(symbol=code, frequency=9, offset=300)
            if raw is None or raw.empty:
                continue

            df = pd.DataFrame({
                "trade_date": [d.strftime("%Y%m%d") for d in raw.index],
                "open": raw["open"].values,
                "close": raw["close"].values,
                "high": raw["high"].values,
                "low": raw["low"].values,
                "vol": raw["vol"].values,
                "amount": raw["amount"].values if "amount" in raw.columns else 0,
            })
            df = df.sort_values("trade_date").reset_index(drop=True)

            if df.empty:
                continue

            bars = [
                {"time": r["trade_date"], "open": r["open"], "high": r["high"],
                 "low": r["low"], "close": r["close"],
                 "volume": r["vol"], "amount": r.get("amount", 0)}
                for _, r in df.iterrows()
            ]
            storage.upsert_kline(code, bars)
            fetched += 1

            # 短暂延迟，防止 IP 被封
            if i > 0 and i % 80 == 0:
                time.sleep(0.03)

        except Exception as e:
            continue

        # 每 100 只输出一次进度
        if (i + 1) % 100 == 0:
            elapsed = time.time() - start_time
            print(f"  [数据缓存] {i+1}/{total} ({fetched} 只新增), {elapsed:.0f}s")

    elapsed = time.time() - start_time
    print(f"[数据缓存] 完成！处理 {total} 只, 新增 {fetched} 只, 耗时 {elapsed:.0f}s")
    stats = storage.kline_stats()
    print(f"[数据缓存] 当前库内: {stats['stocks']} 只, {stats['rows']:,} 行, {stats['date_range']}")


def _build_df_from_cache(code: str, trade_date: str) -> "pd.DataFrame | None":
    """从 SQLite 缓存读取 K 线并计算技术指标。"""
    import pandas as pd

    storage = get_storage()
    end_dt = pd.to_datetime(trade_date)
    start_dt = end_dt - pd.Timedelta(days=300)
    start_str = start_dt.strftime("%Y%m%d")

    kline = storage.get_kline(code, start_str, trade_date)
    if not kline or len(kline) < 60:
        return None

    df = pd.DataFrame(kline)
    df = df.rename(columns={"time": "trade_date", "volume": "vol"})
    if "amount" not in df.columns:
        df["amount"] = 0

    if "open" not in df.columns or df.empty:
        return None

    df = enrich_indicators(df)
    return df


def _refresh_kline_for_codes(codes: list[str]):
    """为指定股票列表拉取最新 K 线数据并写入缓存。

    用于在分析前更新过时的价格数据，确保"至今涨跌幅"反映最新行情。
    每次仅拉取最近 10 条 K 线，通过 INSERT OR IGNORE 去重写入。
    """
    import pandas as pd

    if not codes:
        return

    storage = get_storage()
    quote_client = mootdx_client.get_client()
    refreshed = 0

    for code in codes:
        try:
            raw = quote_client.bars(symbol=code, frequency=9, offset=10)
            if raw is None or raw.empty:
                continue

            df = pd.DataFrame({
                "trade_date": [d.strftime("%Y%m%d") for d in raw.index],
                "open": raw["open"].values,
                "close": raw["close"].values,
                "high": raw["high"].values,
                "low": raw["low"].values,
                "vol": raw["vol"].values,
                "amount": raw["amount"].values if "amount" in raw.columns else 0,
            })
            df = df.sort_values("trade_date").reset_index(drop=True)

            bars = [
                {"time": r["trade_date"], "open": r["open"], "high": r["high"],
                 "low": r["low"], "close": r["close"],
                 "volume": r["vol"], "amount": r.get("amount", 0)}
                for _, r in df.iterrows()
            ]
            storage.upsert_kline(code, bars)
            refreshed += 1
        except Exception:
            continue

    if refreshed > 0:
        print(f"[分析刷新] 已更新 {refreshed} 只股票的 K 线数据")


# ---------------------------------------------------------------------------
# 扫描执行
# ---------------------------------------------------------------------------

def _run_scan(scan_id: str, strategy: str, trade_date: str, top: int):
    """后台运行全市场策略扫描。"""
    global _current_scan_id

    if not _scan_lock.acquire(blocking=False):
        print(f"[扫描] {scan_id}: 已有扫描在运行，跳过")
        return

    _current_scan_id = scan_id
    storage = get_storage()

    try:
        strategy_name = STRATEGIES[strategy][0]
        print(f"\n{'='*60}")
        print(f"  🎯 [{scan_id}] 开始扫描: {strategy_name}")
        print(f"  📅 交易日: {trade_date}")
        print(f"{'='*60}")

        # ---- Step 1: 确保数据就绪 ----
        print(f"\n[Step 1/3] 检查数据缓存...")
        with _data_lock:
            _ensure_kline_cache(trade_date)

        # ---- Step 2: 加载股票池 ----
        print(f"\n[Step 2/3] 加载股票池...")
        universe = get_universe()
        total = len(universe)
        print(f"[Step 2/3] 共 {total} 只 A 股待筛选")
        storage.start_scan(scan_id, strategy, trade_date, total)

        # ---- Step 3: 获取市场环境 ----
        try:
            overview = get_market_overview()
            market_status = get_market_status()
            env = MarketEnv.from_overview(overview, market_status.get("phase", "morning"))
        except Exception:
            env = MarketEnv(
                breadth_ratio=0.5, index_change_pct=0,
                phase="morning", trending=True, bullish=True, bearish=False,
            )
        print(f"[Step 2/3] 市场环境: 涨跌比={env.breadth_ratio:.2f}, 趋势市={env.trending}, 偏多={env.bullish}")

        # ---- Step 3: 逐只筛选 ----
        print(f"\n[Step 3/3] 开始逐只筛选...")
        start_time = time.time()
        results: list[ScreenResult] = []
        processed = 0

        for item in universe:
            code = item["code"]
            name = item["name"]

            try:
                df = _build_df_from_cache(code, trade_date)
                if df is not None:
                    result = screen_stock(df, code, name, strategy, env)
                    if result:
                        results.append(result)
            except Exception as e:
                print(f"  ⚠️  {code} {name} 筛选异常: {e}")

            processed += 1
            if processed % 500 == 0:
                elapsed = time.time() - start_time
                speed = processed / max(elapsed, 1)
                print(f"  [筛选] {processed}/{total} ({processed*100//total}%), "
                      f"入围 {len(results)}, 速度 {speed:.0f} 只/秒")

        elapsed = time.time() - start_time
        print(f"  [筛选] 完成: {processed}/{total}, 入围 {len(results)} 只, 耗时 {elapsed:.0f}s")

        # ---- 排序并取 Top N ----
        results.sort(key=lambda x: x.score, reverse=True)
        top_results = results[:top]

        for i, r in enumerate(top_results):
            r.rank = i + 1

        # ---- 保存结果 ----
        result_dicts = [
            {
                "code": r.code, "name": r.name, "score": r.score, "rank": r.rank,
                "factors_detail": r.factors_detail,
                "signals": r.signals, "metrics": r.metrics,
            }
            for r in top_results
        ]
        storage.save_strategy_results(scan_id, strategy, trade_date, result_dicts)

        duration_ms = int(elapsed * 1000)
        storage.complete_scan(scan_id, len(top_results), duration_ms)

        # 打印 Top 5
        print(f"\n  {'─'*50}")
        print(f"  {'排名':<4} {'代码':<8} {'名称':<8} {'分数':>6}")
        print(f"  {'─'*50}")
        for r in top_results[:5]:
            print(f"  {r.rank:<4} {r.code:<8} {r.name:<8} {r.score:>6.0f}")
        print(f"\n✅ [{scan_id}] {strategy_name} 扫描完成: Top {len(top_results)}/{len(results)} 只, 耗时 {elapsed:.0f}s\n")

    except Exception as e:
        print(f"\n❌ [{scan_id}] 扫描失败: {e}")
        import traceback
        traceback.print_exc()
        storage.fail_scan(scan_id, str(e))
    finally:
        _current_scan_id = None
        _scan_lock.release()


# ==========================================================================
# API 端点
# ==========================================================================

@router.get("/scanning")
def get_scanning_status():
    """查询当前是否有扫描正在运行。"""
    return ok({
        "scanning": is_scanning(),
        "scan_id": _current_scan_id,
    })


@router.get("/status")
def strategy_status():
    """获取策略引擎状态：推荐策略、最近扫描记录。"""
    storage = get_storage()

    try:
        overview = get_market_overview()
        market_status = get_market_status()
        env = MarketEnv.from_overview(overview, market_status.get("phase", "morning"))
    except Exception:
        env = MarketEnv(
            breadth_ratio=0.5, index_change_pct=0,
            phase="morning", trending=True, bullish=True, bearish=False,
        )

    recommended = []
    if env.trending and env.bullish:
        recommended.append({"strategy": "s1", "name": "趋势跟随", "reason": "当前市场趋势偏多，适合顺势而为"})
        recommended.append({"strategy": "s3", "name": "动量突破", "reason": "趋势市中突破信号有效性较高"})
    elif env.trending and env.bearish:
        recommended.append({"strategy": "s2", "name": "底部反转", "reason": "市场偏弱，关注超跌反弹机会"})
    else:
        recommended.append({"strategy": "s1", "name": "趋势跟随", "reason": "震荡市中关注个股独立趋势"})
        recommended.append({"strategy": "s2", "name": "底部反转", "reason": "震荡市中关注超跌标的"})

    recent_scans = storage.get_latest_scans(5)
    kline = storage.kline_stats()

    return ok({
        "market_env": {
            "breadth_ratio": env.breadth_ratio,
            "index_change_pct": env.index_change_pct,
            "trending": env.trending,
            "bullish": env.bullish,
            "bearish": env.bearish,
        },
        "recommended": recommended,
        "strategies": [
            {"key": "s1", "name": "趋势跟随", "description": "顺势而为，捕捉已确立上升趋势中的回调买点"},
            {"key": "s2", "name": "底部反转", "description": "左侧布局，多重确认后的超跌反弹机会"},
            {"key": "s3", "name": "动量突破", "description": "捕捉放量突破关键阻力后的加速行情"},
        ],
        "recent_scans": recent_scans,
        "scanning": is_scanning(),
        "data_coverage": f"{kline['stocks']} 只, {kline['rows']:,} 行",
    })


@router.get("/results/today")
def today_results():
    """获取今天所有已完成的策略筛选结果（用于页面刷新后恢复）。"""
    storage = get_storage()
    trade_date = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d")

    strategies_data: dict[str, dict] = {}
    for key, (name, _) in STRATEGIES.items():
        scan_id = storage.get_latest_scan_id(key, trade_date)
        if scan_id:
            scan_status = storage.get_scan_status(scan_id)
            if scan_status and scan_status["status"] == "completed":
                results = storage.get_strategy_results(scan_id, key)
                strategies_data[key] = {
                    "scan_id": scan_id,
                    "status": "completed",
                    "strategy": key,
                    "strategy_name": name,
                    "trade_date": trade_date,
                    "total": scan_status.get("total_stocks", 0),
                    "matched": scan_status.get("matched_count", 0),
                    "duration_ms": scan_status.get("duration_ms", 0),
                    "results": _enrich_results(results),
                }

    return ok({
        "trade_date": trade_date,
        "strategies": strategies_data,
        "has_any": len(strategies_data) > 0,
    })


@router.get("/history/{strategy}")
def strategy_history(
    strategy: str,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """获取指定策略的历史扫描记录列表。"""
    if strategy not in STRATEGIES:
        return fail(f"未知策略: {strategy}。支持: s1, s2, s3", code=4001)

    storage = get_storage()
    scans = storage.get_strategy_scans(strategy, limit, offset)
    total = storage.count_strategy_scans(strategy)

    return ok({
        "strategy": strategy,
        "strategy_name": STRATEGIES[strategy][0],
        "scans": scans,
        "total": total,
        "limit": limit,
        "offset": offset,
    })


@router.get("/{strategy}")
def run_strategy(
    strategy: str,
    date: str = Query(default="latest"),
    top: int = Query(default=20, ge=1, le=100),
    force: bool = Query(default=False),
):
    """运行策略筛选。

    如果已有当天该策略的扫描结果且非 force，直接返回缓存。
    否则触发后台扫描。
    """
    if strategy not in STRATEGIES:
        return fail(f"未知策略: {strategy}。支持: s1, s2, s3", code=4001)

    if is_scanning():
        return fail("当前已有扫描任务在运行，请等待完成后再试", code=4002)

    storage = get_storage()

    if date == "latest":
        trade_date = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d")
    else:
        trade_date = date

    # 检查已完成缓存
    if not force:
        latest_scan_id = storage.get_latest_scan_id(strategy, trade_date)
        if latest_scan_id:
            scan_status = storage.get_scan_status(latest_scan_id)
            if scan_status and scan_status["status"] == "completed":
                results = storage.get_strategy_results(latest_scan_id, strategy)
                return ok({
                    "scan_id": latest_scan_id,
                    "status": "completed",
                    "strategy": strategy,
                    "strategy_name": STRATEGIES[strategy][0],
                    "trade_date": trade_date,
                    "total": scan_status.get("total_stocks", 0),
                    "matched": scan_status.get("matched_count", 0),
                    "duration_ms": scan_status.get("duration_ms", 0),
                    "results": _enrich_results(results),
                })

    now = datetime.now(timezone(timedelta(hours=8)))
    scan_id = f"{trade_date}_{now.strftime('%H%M%S')}"

    thread = threading.Thread(
        target=_run_scan,
        args=(scan_id, strategy, trade_date, top),
        daemon=True,
        name=f"strategy-scan-{scan_id}",
    )
    thread.start()

    return ok({
        "scan_id": scan_id,
        "status": "running",
        "strategy": strategy,
        "strategy_name": STRATEGIES[strategy][0],
        "trade_date": trade_date,
        "message": f"已触发 {STRATEGIES[strategy][0]} 策略扫描",
    })


@router.get("/scan/{scan_id}")
def get_scan_result(scan_id: str):
    """查询扫描任务状态和结果。"""
    storage = get_storage()
    scan_status = storage.get_scan_status(scan_id)

    if not scan_status:
        return fail("扫描任务不存在", code=4004)

    if scan_status["status"] == "running":
        return ok({
            "scan_id": scan_id,
            "status": "running",
            "strategy": scan_status["strategy"],
            "total_stocks": scan_status["total_stocks"],
        })

    if scan_status["status"] == "failed":
        return ok({
            "scan_id": scan_id,
            "status": "failed",
            "error": scan_status.get("error_message", "未知错误"),
        })

    strategy = scan_status["strategy"]
    results = storage.get_strategy_results(scan_id, strategy)

    return ok({
        "scan_id": scan_id,
        "status": "completed",
        "strategy": strategy,
        "strategy_name": STRATEGIES.get(strategy, ("",))[0],
        "trade_date": scan_status["trade_date"],
        "total": scan_status["total_stocks"],
        "matched": scan_status["matched_count"],
        "duration_ms": scan_status["duration_ms"],
        "results": _enrich_results(results),
    })


@router.delete("/scan/{scan_id}")
def delete_scan_result(scan_id: str):
    """删除指定扫描记录及其结果。"""
    storage = get_storage()
    scan_status = storage.get_scan_status(scan_id)

    if not scan_status:
        return fail("扫描任务不存在", code=4004)

    if scan_status["status"] == "running":
        return fail("扫描正在运行，无法删除", code=4005)

    deleted = storage.delete_scan(scan_id)
    if not deleted:
        return fail("删除失败", code=5000)

    return ok(None, message="已删除")


@router.get("/scan/{scan_id}/consensus")
def get_consensus(scan_id: str):
    """获取指定扫描的多策略共振股票。"""
    storage = get_storage()
    consensus = storage.get_consensus_stocks(scan_id, min_strategies=2)
    return ok(consensus)


@router.get("/scan/{scan_id}/analysis")
def get_scan_analysis(scan_id: str):
    """获取扫描绩效分析：策略至今涨跌幅、统计概览、得分相关性。

    在计算前自动检查并刷新过时的 K 线数据，确保"至今涨跌幅"反映最新行情。
    """
    storage = get_storage()

    # ---- 检查并刷新 kline 数据 ----
    scan = storage.get_scan_status(scan_id)
    if scan and scan["status"] == "completed":
        results = storage.get_strategy_results(scan_id, scan["strategy"])
        codes = [r["code"] for r in results]
        if codes:
            today = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d")
            stale = storage.get_stale_kline_codes(codes, today)
            if stale:
                _refresh_kline_for_codes(stale)

    analysis = storage.get_scan_analysis(scan_id)
    if analysis is None:
        return fail("扫描任务不存在", code=4004)
    return ok(analysis)


@router.get("/results/latest")
def latest_results(
    strategy: str = Query(default="s1"),
    date: str = Query(default=""),
):
    """获取最近的策略筛选结果（快捷方式）。"""
    storage = get_storage()
    if not date:
        date = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d")

    scan_id = storage.get_latest_scan_id(strategy, date)
    if not scan_id:
        return ok({"results": [], "message": "暂无该策略的筛选结果"})

    results = storage.get_strategy_results(scan_id, strategy)
    return ok({
        "scan_id": scan_id,
        "strategy": strategy,
        "trade_date": date,
        "results": _enrich_results(results),
    })


def _enrich_results(results: list[dict]) -> list[dict]:
    enriched = []
    for r in results:
        enriched.append({
            **r,
            "score_display": f"{r['score']:.0f}分",
            "signals_preview": r.get("signals", [])[:3] if isinstance(r.get("signals"), list) else [],
        })
    return enriched
