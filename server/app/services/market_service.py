"""市场概况服务 — 从 mootdx 拉取并缓存至 SQLite。

缓存策略（与详情页统一）：
- 交易时段：始终从 mootdx 同步拉取
- 非交易时段（盘后/周末/节假日）：缓存来自最近一次收盘则用，否则重新拉
"""

from __future__ import annotations

import contextlib
import io
import math
import time
from typing import Any

from app.core.sqlite_storage import get_storage
from app.services.demo_data import INDICES, SECTORS, STOCKS
from app.services.market_calendar import get_market_status, last_close_time
from app.services.mootdx_client import client as mootdx_client

BATCH_SIZE = 80

INDEX_SYMBOLS = [
    ("上证指数", "000001", "000001.SH"),
    ("深证成指", "399001", "399001.SZ"),
    ("创业板指", "399006", "399006.SZ"),
    ("科创 50", "000688", "000688.SH"),
    ("沪深300", "000300", "000300.SH"),
    ("中证500", "000905", "000905.SH"),
]


def get_market_overview() -> dict:
    """获取市场总览（统一规则）。

    - 交易时间 → 始终从 mootdx 同步拉取
    - 非交易时间 → 缓存来自最近一次收盘则用，否则重新拉
    - 失败兜底：mootdx 抽风时返回 ``_demo_overview`` 占位
    """
    storage = get_storage()
    is_trading = get_market_status().get("trading", False)

    if is_trading:
        try:
            data = _build_mootdx_overview()
            storage.set_market_overview(data, "mootdx")
            return data
        except Exception:
            cached = storage.get_market_overview()
            return {k: v for k, v in (cached or {}).items() if k not in ("_cached_at", "_source")} or _demo_overview()

    # 非交易时间
    cached = storage.get_market_overview()
    if cached and storage.is_market_overview_fresh():
        return {k: v for k, v in cached.items() if k not in ("_cached_at", "_source")}

    try:
        data = _build_mootdx_overview()
        storage.set_market_overview(data, "mootdx")
        return data
    except Exception:
        return {k: v for k, v in (cached or {}).items() if k not in ("_cached_at", "_source")} or _demo_overview()


def _build_mootdx_overview() -> dict:
    # stocks / quote / index_bars 各自走独立 slot，互不抢占
    universe = _stock_universe()
    quotes = _stock_quotes(universe)
    if not quotes:
        raise RuntimeError("mootdx 未返回股票快照")

    indices = _index_quotes()
    updated_at = _latest_time(indices, quotes)
    up = sum(1 for item in quotes if item["change"] > 0)
    down = sum(1 for item in quotes if item["change"] < 0)
    flat = max(0, len(quotes) - up - down)
    sorted_by_change = sorted(quotes, key=lambda x: x["changePct"], reverse=True)
    sorted_by_amount = sorted(quotes, key=lambda x: x["amount"], reverse=True)

    return {
        "updatedAt": updated_at,
        "source": "mootdx",
        "coverage": f"mootdx 全市场 A 股 {len(quotes)} 只",
        "indices": indices,
        "breadth": {"up": up, "flat": flat, "down": down},
        "gainers": sorted_by_change[:10],
        "losers": sorted_by_change[-10:][::-1],
        "active": sorted_by_amount[:12],
        "sectors": _sector_summary(quotes),
    }


def _stock_universe() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for market in (0, 1):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            # 走独立 stocks slot
            frame = mootdx_client.stocks(market)
        for row in frame.to_dict("records"):
            code = str(row.get("code", "")).strip()
            name = _clean_name(row.get("name", ""))
            if not _is_a_share(code, market) or not name or "指数" in name:
                continue
            items.append({"code": code, "name": name, "market": _market_code(code, market), "marketName": _market_name(code, market)})
    dedup: dict[str, dict[str, Any]] = {}
    for item in items:
        dedup[item["code"]] = item
    return list(dedup.values())


def _stock_quotes(universe: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_code = {item["code"]: item for item in universe}
    rows: list[dict[str, Any]] = []
    codes = list(by_code)
    for start in range(0, len(codes), BATCH_SIZE):
        batch = codes[start : start + BATCH_SIZE]
        # 走独立 quote slot
        frame = mootdx_client.quote(batch)
        if frame is None or frame.empty:
            continue
        for row in frame.to_dict("records"):
            code = str(row.get("code", "")).strip()
            base = by_code.get(code)
            if not base:
                continue
            price = _num(row.get("price"))
            last_close = _num(row.get("last_close"))
            if price <= 0 or last_close <= 0:
                continue
            change = price - last_close
            amount = _num(row.get("amount"))
            rows.append({
                **base,
                "price": round(price, 2),
                "change": round(change, 2),
                "changePct": round(change / last_close * 100, 2),
                "open": round(_num(row.get("open")), 2),
                "high": round(_num(row.get("high")), 2),
                "low": round(_num(row.get("low")), 2),
                "volume": round(_num(row.get("vol")), 2),
                "amount": round(amount, 2),
                "amountText": _format_amount(amount),
                "marketCap": 0,
                "industry": "",
                "updatedAt": str(row.get("servertime") or ""),
            })
    return rows


def _index_quotes() -> list[dict[str, Any]]:
    indices = []
    for name, symbol, code in INDEX_SYMBOLS:
        # 走独立 index_bars slot
        frame = mootdx_client.index_bars(symbol, frequency=9, offset=2)
        if frame is None or frame.empty:
            continue
        rows = frame.to_dict("records")
        latest = rows[-1]
        previous = rows[-2] if len(rows) > 1 else latest
        close = _num(latest.get("close"))
        prev_close = _num(previous.get("close"))
        change = close - prev_close
        indices.append({
            "name": name,
            "code": code,
            "price": round(close, 2),
            "change": round(change, 2),
            "changePct": round(change / prev_close * 100, 2) if prev_close else 0,
            "open": round(_num(latest.get("open")), 2),
            "high": round(_num(latest.get("high")), 2),
            "low": round(_num(latest.get("low")), 2),
            "volume": round(_num(latest.get("vol")), 2),
            "amount": round(_num(latest.get("amount")), 2),
            "amountText": _format_amount(_num(latest.get("amount"))),
            "updatedAt": str(latest.get("datetime") or ""),
            "upCount": int(_num(latest.get("up_count"))),
            "downCount": int(_num(latest.get("down_count"))),
        })
    if not indices:
        raise RuntimeError("mootdx 未返回指数数据")
    return indices


def _sector_summary(quotes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    industry_by_code = {item["code"]: item.get("industry", "") for item in STOCKS}
    groups: dict[str, list[dict[str, Any]]] = {}
    for quote in quotes:
        industry = industry_by_code.get(quote["code"])
        if not industry:
            continue
        groups.setdefault(industry, []).append(quote)

    sectors = []
    for name, items in groups.items():
        total_amount = sum(item["amount"] for item in items)
        weighted_change = sum(item["changePct"] * max(item["amount"], 1) for item in items) / max(total_amount, 1)
        sectors.append({"name": name, "changePct": round(weighted_change, 2), "count": len(items), "amountText": _format_amount(total_amount)})
    return sorted(sectors, key=lambda x: x["changePct"], reverse=True)[:12]


def _demo_overview() -> dict:
    gainers = sorted(STOCKS, key=lambda x: x["changePct"], reverse=True)[:5]
    losers = sorted(STOCKS, key=lambda x: x["changePct"])[:5]
    active = sorted(({**item, "amount": item["volume"] * item["price"] * 10000, "amountText": _format_amount(item["volume"] * item["price"] * 10000)} for item in STOCKS), key=lambda x: x["amount"], reverse=True)[:12]
    return {
        "updatedAt": "演示数据 · mootdx 暂不可用",
        "source": "demo",
        "coverage": "演示股票池",
        "indices": INDICES,
        "breadth": {"up": 2847, "flat": 312, "down": 1932},
        "gainers": gainers,
        "losers": losers,
        "active": active,
        "sectors": SECTORS,
    }


def _latest_time(indices: list[dict[str, Any]], quotes: list[dict[str, Any]]) -> str:
    for index in indices:
        if index.get("updatedAt"):
            return str(index["updatedAt"])
    for quote in quotes:
        if quote.get("updatedAt"):
            return str(quote["updatedAt"])
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _is_a_share(code: str, market: int) -> bool:
    if market == 0:
        return code.startswith(("000", "001", "002", "003", "300", "301"))
    return code.startswith(("600", "601", "603", "605", "688"))


def _market_code(code: str, market: int) -> str:
    if code.startswith(("300", "301")):
        return "cyb"
    if code.startswith("688"):
        return "kcb"
    return "sz" if market == 0 else "sh"


def _market_name(code: str, market: int) -> str:
    if code.startswith(("300", "301")):
        return "创业板"
    if code.startswith("688"):
        return "科创板"
    return "深市主板" if market == 0 else "沪市主板"


def _clean_name(value: Any) -> str:
    return str(value or "").replace("\x00", "").strip()


def _num(value: Any) -> float:
    try:
        number = float(value)
        if math.isnan(number) or math.isinf(number):
            return 0.0
        return number
    except (TypeError, ValueError):
        return 0.0


def _format_amount(value: float) -> str:
    if value >= 100_000_000:
        return f"{value / 100_000_000:.1f}亿"
    if value >= 10_000:
        return f"{value / 10_000:.1f}万"
    return f"{value:.0f}"
