"""A 股股票池：mootdx 全量拉取 + SQLite 缓存。

- 每天刷新一次（cachedAt + 86400s 判定失效）
- 拉取失败时回退到旧缓存；完全没缓存时回退到 demo 数据
- 仅保留 A 股代码前缀，过滤掉指数/基金/债券/期权/可转债等非股票条目
"""

from __future__ import annotations

import contextlib
import io
import re
import threading
import time
from typing import Any

from app.core.sqlite_storage import get_storage
from app.services.demo_data import STOCKS
from app.services.mootdx_client import client as mootdx_client

CACHE_TTL_SECONDS = 24 * 60 * 60  # 一天

_background_lock = threading.Lock()

# 沪市主板、科创板；深市主板、创业板
SZ_PREFIXES = ("000", "001", "002", "003", "300", "301")
SH_PREFIXES = ("600", "601", "603", "605", "688")

_NON_STOCK_KEYWORDS = (
    "指数", "板块指数", "板块Ａ", "板块Ｂ", "Ａ股指数", "Ｂ股指数",
    "基金", "货币", "回购", "债", "可转债", "转债",
    "ETF", "ＬＯＦ", "ＦＯＦ", "期权", "购汇",
    "优先股", "标准券", "活筹", "总市值", "流通市值", "平均股价",
    "Ｂ股", "主板Ｂ", "创业板Ｂ",
    "退",
)


def _market_for(code: str) -> tuple[str, str] | None:
    if code.startswith(("300", "301")):
        return "cyb", "创业板"
    if code.startswith("688"):
        return "kcb", "科创板"
    if code.startswith(SH_PREFIXES):
        return "sh", "沪市主板"
    if code.startswith(SZ_PREFIXES):
        return "sz", "深市主板"
    return None


def _is_a_share(code: str, name: str) -> bool:
    market_info = _market_for(code)
    if market_info is None:
        return False
    if not name or len(name) > 12:
        return False
    if any(keyword in name for keyword in _NON_STOCK_KEYWORDS):
        return False
    return True


def _clean_name(value: Any) -> str:
    text = str(value or "").replace("\x00", "").strip()
    text = re.sub(r"\s+", "", text)
    return text


def _fetch_from_mootdx() -> list[dict[str, str]]:
    print("[股票池] 正在从通达信拉取 A 股列表...")
    items: list[dict[str, str]] = []
    seen: set[str] = set()
    for market in (0, 1):
        label = "深市" if market == 0 else "沪市"
        print(f"[股票池]   拉取{label}列表...")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            # 走独立 stocks slot，与详情页并行接口互不抢占
            frame = mootdx_client.stocks(market)
        if frame is None or frame.empty:
            print(f"[股票池]   {label}返回空")
            continue
        for row in frame.to_dict("records"):
            code = str(row.get("code", "")).strip()
            name = _clean_name(row.get("name", ""))
            if code in seen or not _is_a_share(code, name):
                continue
            market_code, market_name = _market_for(code) or ("sh", "沪市主板")
            items.append({
                "code": code,
                "name": name,
                "market": market_code,
                "marketName": market_name,
                "industry": "",
            })
            seen.add(code)
        print(f"[股票池]   {label}解析完成，当前累计 {len(items)} 只")
    items.sort(key=lambda item: item["code"])
    print(f"[股票池] 拉取完成: 共 {len(items)} 只 A 股")
    return items


def _demo_fallback() -> list[dict[str, str]]:
    return [
        {
            "code": item["code"],
            "name": item["name"],
            "market": item["market"],
            "marketName": item["marketName"],
            "industry": item.get("industry", ""),
        }
        for item in STOCKS
    ]


def _persist(items: list[dict[str, str]]) -> None:
    storage = get_storage()
    storage.upsert_universe(items)
    storage.set_universe_status(len(items), "mootdx")


def get_universe(force_refresh: bool = False) -> list[dict[str, str]]:
    """返回股票池；缓存 24h 失效，失效时返回旧缓存并后台触发刷新。"""
    storage = get_storage()
    status = storage.get_universe_status()
    fresh = status["fresh"]

    if force_refresh:
        return _refresh_now()

    if fresh:
        return storage.get_universe()

    # 缓存过期或不存在：返回当前数据（缓存或 demo），后台异步刷新
    _schedule_background_refresh()
    cached = storage.get_universe()
    if cached:
        return cached

    # 首次没缓存时同步尝试拉一次，超时回退 demo
    try:
        items = _fetch_from_mootdx()
        if items:
            _persist(items)
            return items
    except Exception:
        pass
    return _demo_fallback()


def _refresh_now() -> list[dict[str, str]]:
    items = _fetch_from_mootdx()
    if items:
        _persist(items)
        return items
    cached = get_storage().get_universe()
    if cached:
        return cached
    return _demo_fallback()


def _schedule_background_refresh() -> None:
    if not _background_lock.acquire(blocking=False):
        return
    thread = threading.Thread(target=_background_refresh, daemon=True, name="stock-universe-refresh")
    thread.start()


def _background_refresh() -> None:
    try:
        items = _fetch_from_mootdx()
        if items:
            _persist(items)
    except Exception:
        pass
    finally:
        _background_lock.release()


def get_universe_status() -> dict[str, Any]:
    return get_storage().get_universe_status()
