from __future__ import annotations

import math
import time
from datetime import datetime, timedelta
from typing import Any

from app.core.sqlite_storage import get_storage
from app.services.demo_data import MINUTE_VALUES, STOCKS
from app.services.market_calendar import get_market_status
from app.services.mootdx_client import client as mootdx_client
from app.services.stock_universe import get_universe, _market_for

BATCH_SIZE = 80

STATIC_BY_CODE = {item["code"]: item for item in STOCKS}


def _fallback_stock(code: str) -> dict:
    """无可用行情时构造占位记录，决不返回错误股票的数据。

    之前会从 STATIC_BY_CODE 命中其他 demo 股票，造成"代码对名字错"。
    现在只用 universe 提供真实股票元数据；找不到时 name 留空，
    由前端展示"暂未获取到行情"。
    """
    universe = {item["code"]: item for item in get_universe()}
    meta = universe.get(code)
    if meta:
        return {
            "code": code,
            "name": meta["name"],
            "market": meta["market"],
            "marketName": meta["marketName"],
            "price": 0,
            "change": 0,
            "changePct": 0,
            "volume": 0,
            "marketCap": 0,
            "industry": meta.get("industry") or "",
        }
    market_info = _market_for(code)
    return {
        "code": code,
        "name": "",  # 留空让前端展示"暂未获取"
        "market": market_info[0] if market_info else "sh",
        "marketName": market_info[1] if market_info else "沪市主板",
        "price": 0,
        "change": 0,
        "changePct": 0,
        "volume": 0,
        "marketCap": 0,
        "industry": "",
    }


KLINE_FREQUENCY = {
    "1m": 8,
    "5m": 0,
    "15m": 1,
    "30m": 2,
    "60m": 3,
    "day": 9,
    "week": 5,
    "mon": 6,
}

KLINE_OFFSET = {
    "1m": 240,
    "5m": 240,
    "15m": 240,
    "30m": 200,
    "60m": 240,
    "120m": 240,
    "day": 240,
    "week": 240,
    "mon": 120,
}


def get_stock_list(
    q: str, market: str, sort: str, order: str,
    page: int = 1, page_size: int = 20,
) -> dict:
    """返回分页后的股票列表，仅对当前页加载行情数据。"""
    universe = get_universe()
    keyword = (q or "").strip().lower()

    # 过滤
    filtered: list[dict] = []
    for item in universe:
        if market != "all" and item["market"] != market:
            continue
        if keyword and keyword not in item["code"] and keyword not in item["name"].lower():
            continue
        filtered.append(item)

    # 预排序（用 universe 自带字段不够，需要先粗略排序减少后续合并成本）
    # 这里直接用 code 排序作为默认，真实排序在 enrich 之后
    filtered.sort(key=lambda x: x["code"])

    total = len(filtered)

    # 分页切片
    start = (page - 1) * page_size
    page_items = filtered[start : start + page_size]

    # 仅对当前页股票加载行情数据
    quote_lookup = _quote_lookup()
    enriched: list[dict] = []
    for item in page_items:
        quote = quote_lookup.get(item["code"])
        if quote:
            enriched.append({**item, **quote})
        else:
            enriched.append({**item, "price": 0, "change": 0, "changePct": 0,
                             "volume": 0, "marketCap": 0, "industry": item.get("industry", "")})

    enriched = _sort_items(enriched, sort, order)

    return {
        "items": enriched,
        "total": total,
        "page": page,
        "pageSize": page_size,
    }


def _quote_lookup() -> dict[str, dict]:
    """从 SQLite 缓存里取 code → {price, change, changePct} 子集。"""
    return get_storage().get_quote_lookup()


def _market_for_code(code: str) -> tuple[str, str]:
    """根据代码推导 (market, marketName)，无需网络调用。"""
    mkt = _market_int(code)
    return (_market_code(code, mkt), _market_name(code, mkt))


def _is_trading_now() -> bool:
    """判断当前是否在交易时段。"""
    try:
        ms = get_market_status()
        return ms.get("trading", False)
    except Exception:
        return False


def get_stock_snapshot(code: str) -> dict:
    """获取股票行情快照（统一规则）。

    - 交易时间 → 始终从 mootdx 拉取，同时更新缓存
    - 非交易时间 → 缓存来自最近一次收盘则用缓存，否则拉取
    - 失败兜底：mootdx 抽风时返回 ``_fallback_stock``
    """
    storage = get_storage()

    if not _is_trading_now():
        cached = storage.get_cached_quote(code)
        if cached and storage.is_quote_fresh(code):
            cached["market"] = cached.get("market") or _market_for_code(code)[0]
            cached["marketName"] = cached.get("marketName") or _market_for_code(code)[1]
            return cached

    try:
        # 走独立 slot：quote / stocks 各占一个 socket，与详情页并行接口互不串扰
        row = _quote_row(code)
        meta = _stock_meta(code)
        stock = _quote_to_stock(row, meta)
        # 写入缓存（无论是否交易时段），供后续非交易时段复用
        storage.upsert_single_quote(stock)
        return stock
    except Exception:
        return _fallback_stock(code)


def get_stock_detail(code: str) -> dict:
    """获取股票详情（统一规则）。

    - 交易时间 → 始终从 mootdx 同步拉取，不读缓存
    - 非交易时间 → 缓存来自最近一次收盘时刻则使用缓存，否则重新拉
    - 失败兜底：mootdx 抽风时返回 ``_fallback_stock`` 占位（name 为空，UI 显示"暂未获取"）
    """
    if _is_trading_now():
        return _build_detail_fresh(code)

    cached = get_storage().get_cached_quote(code)
    if cached is not None and get_storage().is_quote_fresh(code):
        return _build_detail_from_cache(code, cached)

    return _build_detail_fresh(code)


def _build_detail_from_cache(code: str, cached: dict) -> dict:
    """走缓存 + 实时 row（量比/PE/涨速）的快速路径。

    非交易时间调用：以缓存为基础，用 row 补充可能缺失或为 0 的
    open / high / low 字段（TDX 实时行情偶有字段缺失，分时数据反而是全的）。
    row 的非零值优先于缓存的零值或空值。
    """
    try:
        # quote / stocks / bars 各自走独立 slot
        row = _quote_row(code)
        meta = _stock_meta(code)
        stock = dict(cached)
        stock["market"] = meta.get("market") or stock.get("market") or _market_for_code(code)[0]
        stock["marketName"] = meta.get("marketName") or stock.get("marketName") or _market_for_code(code)[1]
        # 合并 row 中有效的 open/high/low（行数据比缓存更新，避免缓存里的 0 或 None）
        for field in ("open", "high", "low"):
            raw = row.get(field)
            if raw is None:
                continue
            val = _num(raw)
            if val > 0:
                stock[field] = round(val, 2)
            elif stock.get(field) is None or _num(stock.get(field, 0)) <= 0:
                # row 也是 0，但缓存同样缺失 → 保留 row 的 0（等下次更新）
                stock[field] = round(val, 2) if stock.get(field) is not None else None
        bars = _daily_bars(code)
        payload = _detail_from_quote(stock, row, bars)
        return payload
    except Exception:
        # mootdx 拉不到 → 仍返回缓存，指标字段大多显示"暂缺"
        stock = dict(cached)
        stock.setdefault("market", _market_for_code(code)[0])
        stock.setdefault("marketName", _market_for_code(code)[1])
        return _detail_from_quote(stock, {}, [])


def _build_detail_fresh(code: str) -> dict:
    """同步从 mootdx 拉取，写入缓存后返回（首屏不能空白 + 缓存缺失补救）。"""
    try:
        # quote / stocks / bars 各自走独立 slot
        row = _quote_row(code)
        meta = _stock_meta(code)
        stock = _quote_to_stock(row, meta)
        get_storage().upsert_single_quote(stock)
        bars = _daily_bars(code)
        return _detail_from_quote(stock, row, bars)
    except Exception:
        stock = _fallback_stock(code)
        if stock.get("name"):
            return _demo_detail(stock)
        return {
            **stock,
            "metrics": [],
            "profile": {},
            "finance": [],
            "summary": "暂未获取到行情",
        }


def get_minute_points(code: str) -> list[dict]:
    try:
        # 尝试最近几个交易日，避免周末/节假日直接回退到合成数据
        frame = None
        for offset in range(5):
            date_str = (datetime.now() - timedelta(days=offset)).strftime("%Y%m%d")
            # 走独立 minutes slot
            frame = mootdx_client.minutes(code, date_str)
            if frame is not None and not frame.empty:
                break
        if frame is None or frame.empty:
            raise RuntimeError("mootdx 未返回分时数据")
        points = []
        total = len(frame)
        for index, row in enumerate(frame.to_dict("records")):
            price = _num(row.get("price") or row.get("close"))
            if price > 0:
                time_str = _format_minute_time(row.get("time") or row.get("datetime"))
                if not time_str:
                    # mootdx 分钟数据无 time 列，按索引生成 A 股交易时间
                    time_str = _format_minute_time_from_index(index, total)
                points.append({"index": index, "time": time_str, "price": round(price, 2)})
        if points:
            return points
        raise RuntimeError("mootdx 分时数据为空")
    except Exception:
        snapshot = get_stock_snapshot(code)
        return _synthetic_minute_points(snapshot["price"], snapshot.get("change", 0))


def get_kline(code: str, period: str) -> list[dict]:
    try:
        frequency = KLINE_FREQUENCY[period]
        offset = KLINE_OFFSET[period]
        if period == "120m":
            bars = _fetch_bars(code, KLINE_FREQUENCY["60m"], offset * 2)
            bars = _aggregate_bars(bars, 2, "60m")
        else:
            bars = _fetch_bars(code, frequency, offset)
        if not bars:
            raise RuntimeError("mootdx K 线数据为空")
        return bars
    except Exception:
        snapshot = get_stock_snapshot(code)
        return _synthetic_kline(snapshot["price"], snapshot.get("change", 0), period)


def _stock_list_source() -> list[dict]:
    """股票列表数据源（统一规则）。

    - 交易时间 → 直接从 mootdx 拉
    - 非交易时间 → 缓存来自最近一次收盘则用缓存，否则重新拉
    - 失败兜底：mootdx 抽风时返回 ``STOCKS`` 静态数据
    """
    storage = get_storage()
    if _is_trading_now():
        try:
            items = _real_stock_list()
            storage.upsert_quotes(items)
            return items
        except Exception:
            cached = storage.get_quotes()
            return cached if cached else STOCKS

    if storage.is_quotes_fresh():
        return storage.get_quotes()
    try:
        items = _real_stock_list()
        storage.upsert_quotes(items)
        return items
    except Exception:
        cached = storage.get_quotes()
        return cached if cached else STOCKS


def _real_stock_list() -> list[dict]:
    rows = []
    codes = list(STATIC_BY_CODE)
    for start in range(0, len(codes), BATCH_SIZE):
        # 批量 quotes 走独立 quote slot
        frame = mootdx_client.quote(codes[start : start + BATCH_SIZE])
        if frame is None or frame.empty:
            continue
        rows.extend(frame.to_dict("records"))
    items = []
    for row in rows:
        code = str(row.get("code", "")).strip()
        meta = STATIC_BY_CODE.get(code)
        if meta:
            items.append(_quote_to_stock(row, meta))
    if not items:
        raise RuntimeError("mootdx 未返回股票列表行情")
    return items


def _quote_row(code: str) -> dict[str, Any]:
    """拉取单只股票行情行。走 quote slot。"""
    frame = mootdx_client.quote(code)
    if frame is None or frame.empty:
        raise RuntimeError("mootdx 未返回股票行情")
    return frame.to_dict("records")[0]


def _stock_meta(code: str) -> dict[str, Any]:
    """获取单只股票元数据（名称/市场/行业）。

    优先查 STATIC_BY_CODE（demo 股票），其次查 SQLite 缓存的 universe
    （后台每天自动刷新，毫秒级），不再走 mootdx 网络调用。
    """
    static = STATIC_BY_CODE.get(code)
    if static:
        return static

    # 从 SQLite 缓存的股票池查（每天后台自动刷新，不走网络）
    from app.services.stock_universe import get_universe as _cached_universe

    universe = _cached_universe()  # 首次调用可能同步拉取，后续命中 SQLite
    # 构建 code→item 查找表（O(n) 仅一次，后续请求直接复用 universe list）
    for item in universe:
        if item["code"] == code:
            return {
                "code": code,
                "name": item["name"],
                "market": item["market"],
                "marketName": item["marketName"],
                "marketCap": item.get("marketCap", 0),
                "industry": item.get("industry", ""),
            }

    # 兜底：universe 中也没有（极小概率，可能是新上市或代码错误）
    market = _market_int(code)
    return {
        "code": code,
        "name": f"股票 {code}",
        "market": _market_code(code, market),
        "marketName": _market_name(code, market),
        "marketCap": 0,
        "industry": "",
    }


def _quote_to_stock(row: dict[str, Any], meta: dict[str, Any]) -> dict:
    price = _num(row.get("price"))
    last_close = _num(row.get("last_close"))
    if price <= 0:
        price = _num(row.get("close")) or _num(meta.get("price"))
    if last_close <= 0:
        last_close = price - _num(meta.get("change")) if meta.get("change") is not None else price
    change = price - last_close if last_close > 0 else _num(meta.get("change"))
    amount = _num(row.get("amount"))
    # open/high/low: 仅当 mootdx 实际返回该字段时才取值，否则返回 None
    # 避免 _num(None) → 0.0 导致前端用 || 做 fallback 时展示错误数据
    raw_open = row.get("open")
    raw_high = row.get("high")
    raw_low = row.get("low")

    return {
        "code": str(row.get("code") or meta["code"]).strip(),
        "name": meta.get("name") or str(row.get("name") or meta["code"]).strip(),
        "market": meta.get("market") or _market_code(str(meta["code"]), _market_int(str(meta["code"]))),
        "marketName": meta.get("marketName") or _market_name(str(meta["code"]), _market_int(str(meta["code"]))),
        "price": round(price, 2),
        "change": round(change, 2),
        "changePct": round(change / last_close * 100, 2) if last_close else 0,
        "volume": round(_num(row.get("vol")) / 10000, 1),
        "open": round(_num(raw_open), 2) if raw_open is not None else None,
        "high": round(_num(raw_high), 2) if raw_high is not None else None,
        "low": round(_num(raw_low), 2) if raw_low is not None else None,
        "amount": round(amount, 2),
        "amountText": _format_amount(amount),
        "marketCap": _num(meta.get("marketCap")),
        "industry": meta.get("industry") or "--",
        "updatedAt": str(row.get("servertime") or ""),
    }


def _detail_from_quote(stock: dict, row: dict[str, Any], bars: list[dict[str, Any]]) -> dict:
    previous_close = round(stock["price"] - stock["change"], 2)
    week_high, week_low = _week_range(stock["price"], bars)
    vol_ratio = _compute_vol_ratio(_num(row.get("vol")), bars)
    pe = _extract_pe(row)
    turnover = _compute_turnover(stock["code"], _num(row.get("vol")))
    speed = _extract_speed(row)
    avg_price = _compute_avg_price(row)
    fin = _get_cached_finance(stock["code"])

    # 财务数据
    zongguben = _num(fin.get("zongguben")) if fin else 0
    liutongguben = _num(fin.get("liutongguben")) if fin else 0
    book_value = round(_num(fin.get("meigujingzichan")), 2) if fin else 0
    shareholders = int(_num(fin.get("gudongrenshu"))) if fin else 0
    ipo_date_raw = fin.get("ipo_date") if fin else 0

    return {
        **stock,
        "updatedAt": stock.get("updatedAt") or time.strftime("%Y-%m-%d %H:%M:%S"),
        "metrics": [
            {"label": "开盘", "value": _disp(stock.get("open"), stock["price"]), "sub": _metric_sub(stock.get("open"), previous_close)},
            {"label": "最高", "value": _disp(stock.get("high"), stock["price"]), "sub": _metric_sub(stock.get("high"), previous_close)},
            {"label": "最低", "value": _disp(stock.get("low"), stock["price"]), "sub": _metric_sub(stock.get("low"), previous_close)},
            {"label": "昨收", "value": previous_close, "sub": "-"},
            {"label": "成交量", "value": f"{stock['volume']:.1f}万手", "sub": "mootdx"},
            {"label": "成交额", "value": stock.get("amountText") or _format_amount(_num(row.get("amount"))), "sub": "mootdx"},
            {"label": "量比", "value": f"{vol_ratio:.2f}" if vol_ratio is not None else "--", "sub": vol_ratio_sub(vol_ratio) if vol_ratio is not None else "暂缺"},
            {"label": "换手率", "value": f"{turnover:.2f}%" if turnover is not None else "--", "sub": "mootdx" if turnover is not None else "暂缺"},
            {"label": "市盈率", "value": f"{pe:.2f}" if pe is not None else "--", "sub": "静态" if pe is not None else "暂缺"},
            {"label": "均价", "value": f"{avg_price:.2f}" if avg_price is not None else "--", "sub": "成交额/成交量" if avg_price is not None else "暂缺"},
            {"label": "涨速", "value": f"{speed:.2f}%" if speed is not None else "--", "sub": "5分钟涨速" if speed is not None else "暂缺"},
        ],
        "profile": {
            "industry": stock.get("industry") or "--",
            "board": stock.get("marketName") or "--",
            "marketCap": _market_cap_text(stock.get("marketCap")),
            "floatMarketCap": _market_cap_text(stock.get("marketCap")),
            "shares": _format_shares(zongguben),
            "floatShares": _format_shares(liutongguben),
            "bookValue": f"{book_value:.2f}元" if book_value > 0 else "--",
            "shareholders": f"{shareholders / 10000:.2f}万户" if shareholders > 0 else "--",
            "ipoDate": _format_ipo_date(ipo_date_raw),
            "weekHigh52": week_high,
            "weekLow52": week_low,
        },
        "finance": _build_finance_sections(fin) if fin else [],
        "summary": f"{stock['name']}行情快照来自 mootdx。行业、市值、财务等资料在本地静态池缺失时显示为占位值，后续可接入财务与公司资料源补全。",
    }


def _demo_detail(stock: dict) -> dict:
    previous_close = round(stock["price"] - stock["change"], 2)
    return {
        **stock,
        "updatedAt": "演示数据 · mootdx 暂不可用",
        "metrics": [
            {"label": "开盘", "value": round(previous_close * 1.0023, 2), "sub": "+0.27%"},
            {"label": "最高", "value": round(stock["price"] * 1.0038, 2), "sub": "+0.88%"},
            {"label": "最低", "value": round(previous_close * 0.9982, 2), "sub": "-0.18%"},
            {"label": "昨收", "value": previous_close, "sub": "-"},
            {"label": "成交量", "value": f"{stock['volume']:.1f}万手", "sub": "-"},
            {"label": "成交额", "value": f"{stock['volume'] * stock['price'] / 10000:.1f}亿", "sub": "-"},
            {"label": "量比", "value": "1.08", "sub": "正常"},
            {"label": "换手率", "value": "0.28%", "sub": "-"},
            {"label": "市盈率", "value": "32.6倍", "sub": "TTM"},
            {"label": "均价", "value": f"{previous_close * 0.998:.2f}", "sub": "-"},
            {"label": "涨速", "value": "0.05%", "sub": "-"},
        ],
        "profile": {
            "industry": stock["industry"],
            "board": stock["marketName"],
            "marketCap": f"{stock['marketCap']:,.0f} 亿",
            "floatMarketCap": f"{stock['marketCap']:,.0f} 亿",
            "shares": "12.56 亿股",
            "floatShares": "12.56 亿股",
            "bookValue": "216.32元",
            "shareholders": "24.32万户",
            "ipoDate": "2001-08-27",
            "weekHigh52": round(stock["price"] * 1.1, 2),
            "weekLow52": round(stock["price"] * 0.85, 2),
        },
        "finance": [
            {
                "title": "公司概况",
                "rows": [
                    {"label": "上市日期", "value": "2001-08-27"},
                    {"label": "股东人数", "value": "24.32万户"},
                    {"label": "每股净资产", "value": "216.32元"},
                ],
            },
            {
                "title": "股本结构",
                "rows": [
                    {"label": "总股本", "value": "12.56亿股"},
                    {"label": "流通股本", "value": "12.56亿股"},
                    {"label": "国家股", "value": "--"},
                    {"label": "法人股", "value": "--"},
                ],
            },
            {
                "title": "利润表",
                "rows": [
                    {"label": "主营收入", "value": "5390.92亿"},
                    {"label": "净利润", "value": "2724.25亿"},
                    {"label": "营业利润", "value": "3753.70亿"},
                ],
            },
            {
                "title": "财务比率",
                "rows": [
                    {"label": "净利率", "value": "50.54%"},
                    {"label": "ROE", "value": "10.06%"},
                ],
            },
        ],
        "summary": f"{stock['name']}是{stock['industry']}行业代表公司。本页面使用本地演示行情与结构化指标，mootdx 不可用时仍可完整体验查询、自选和详情流程。",
    }


def _daily_bars(code: str) -> list[dict[str, Any]]:
    """拉取日 K 线。走 bars slot（与分钟线/股票列表互不抢占）。"""
    try:
        frame = mootdx_client.bars(code, frequency=9, offset=260)
        if frame is None or frame.empty:
            return []
        return frame.to_dict("records")
    except Exception:
        return []


def _week_range(price: float, bars: list[dict[str, Any]]) -> tuple[float, float]:
    highs = [_num(item.get("high")) for item in bars if _num(item.get("high")) > 0]
    lows = [_num(item.get("low")) for item in bars if _num(item.get("low")) > 0]
    if highs and lows:
        return round(max(highs), 2), round(min(lows), 2)
    return round(price * 1.1, 2), round(price * 0.85, 2)


def _compute_vol_ratio(today_vol: float, bars: list[dict[str, Any]]) -> float | None:
    """量比 = 今日总成交量 / (过去5日均量 × 已交易分钟 / 240)"""
    if today_vol <= 0 or len(bars) < 5:
        return None
    recent = [_num(b.get("vol")) or _num(b.get("volume")) for b in bars[-6:-1]]
    recent = [v for v in recent if v > 0]
    if len(recent) < 3:
        return None
    avg_vol = sum(recent) / len(recent)
    if avg_vol <= 0:
        return None
    traded = _traded_minutes_today()
    if traded is None or traded < 1:
        return None
    return round(today_vol / (avg_vol * traded / 240), 2)


def _traded_minutes_today() -> float | None:
    """A股今日已交易分钟数 (09:30起，扣除午休)"""
    now = datetime.now()
    morning_start = now.replace(hour=9, minute=30, second=0, microsecond=0)
    morning_end = now.replace(hour=11, minute=30, second=0, microsecond=0)
    afternoon_start = now.replace(hour=13, minute=0, second=0, microsecond=0)
    afternoon_end = now.replace(hour=15, minute=0, second=0, microsecond=0)
    if now < morning_start:
        return None
    if now <= morning_end:
        return (now - morning_start).total_seconds() / 60
    if now < afternoon_start:
        return 120.0
    if now <= afternoon_end:
        return 120.0 + (now - afternoon_start).total_seconds() / 60
    return 240.0


def vol_ratio_sub(value: float | None) -> str:
    """量比 sub 文字"""
    if value is None:
        return "暂缺"
    if value < 0.5:
        return "极度缩量"
    if value < 0.8:
        return "缩量"
    if value <= 1.2:
        return "正常"
    if value <= 2.0:
        return "放量"
    return "巨量"


# ---- 财务数据缓存 (流通股本变动不频繁) ----
_finance_cache: dict[str, dict] = {}


def _get_cached_finance(code: str) -> dict | None:
    """带缓存的财务信息查询（走独立 finance_info slot）"""
    if code in _finance_cache:
        return _finance_cache[code]
    try:
        market = 1 if code.startswith("6") else 0
        fin = mootdx_client.finance_info(market, code)
        if fin:
            _finance_cache[code] = fin
            return fin
    except Exception:
        pass
    return None


def _extract_pe(row: dict[str, Any]) -> float | None:
    """从行情数据提取静态市盈率 (reversed_bytes4 / 100)"""
    rb4 = row.get("reversed_bytes4")
    if rb4 is None:
        return None
    if isinstance(rb4, (list, tuple)):
        val = rb4[0] if len(rb4) > 0 else 0
    else:
        val = rb4
    if val <= 0:
        return None
    return round(val / 100, 2)


def _compute_turnover(code: str, vol_shou: float) -> float | None:
    """换手率 = 成交量(手) / 流通股本(手) × 100%"""
    if vol_shou <= 0:
        return None
    fin = _get_cached_finance(code)
    if not fin:
        return None
    liutong_gu = _num(fin.get("liutongguben"))
    if liutong_gu <= 0:
        return None
    liutong_shou = liutong_gu / 100  # 股 -> 手
    return round(vol_shou / liutong_shou * 100, 2)


def _extract_speed(row: dict[str, Any]) -> float | None:
    """涨速 = reversed_bytes9 (已解码为百分比)"""
    val = row.get("reversed_bytes9")
    if val is None:
        return None
    return round(float(val), 2)


def _compute_avg_price(row: dict[str, Any]) -> float | None:
    """均价 = 成交额 / 成交量(股)"""
    amount = _num(row.get("amount"))
    vol_shou = _num(row.get("vol"))
    if amount > 0 and vol_shou > 0:
        # amount in元, vol in手 = 100股, 均价 = amount / (vol * 100)
        return round(amount / (vol_shou * 100), 2)
    return None


def _format_shares(shares: float) -> str:
    """格式化股本"""
    if shares <= 0:
        return "--"
    yi = shares / 1e8
    if yi >= 1:
        return f"{yi:.2f}亿股"
    wan = shares / 1e4
    return f"{wan:.0f}万股"


def _format_ipo_date(raw: int) -> str:
    """格式化上市日期 YYYYMMDD -> YYYY-MM-DD"""
    if not raw or raw <= 0:
        return "--"
    s = str(int(raw))
    if len(s) == 8:
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return s


def _build_finance_sections(fin: dict) -> list[dict]:
    """将 TDX 财务数据转为 F10 分组展示"""

    def yi(val: Any, decimals: int = 2) -> str:
        n = _num(val)
        return f"{n / 1e8:.{decimals}f}亿" if n > 0 else "--"

    sections: list[dict] = [
        {
            "title": "公司概况",
            "rows": [
                {"label": "上市日期", "value": _format_ipo_date(fin.get("ipo_date"))},
                {"label": "股东人数", "value": f"{int(_num(fin.get('gudongrenshu'))) / 10000:.2f}万户" if _num(fin.get("gudongrenshu")) > 0 else "--"},
                {"label": "每股净资产", "value": f"{_num(fin.get('meigujingzichan')):.2f}元" if _num(fin.get("meigujingzichan")) > 0 else "--"},
            ],
        },
        {
            "title": "股本结构",
            "rows": [
                {"label": "总股本", "value": _format_shares(_num(fin.get("zongguben")))},
                {"label": "流通股本", "value": _format_shares(_num(fin.get("liutongguben")))},
                {"label": "国家股", "value": _format_shares(_num(fin.get("guojiagu")))},
                {"label": "法人股", "value": _format_shares(_num(fin.get("farengu")))},
                {"label": "B股", "value": _format_shares(_num(fin.get("bgu")))},
                {"label": "H股", "value": _format_shares(_num(fin.get("hgu")))},
            ],
        },
        {
            "title": "利润表",
            "rows": [
                {"label": "主营收入", "value": yi(fin.get("zhuyingshouru"))},
                {"label": "主营利润", "value": yi(fin.get("zhuyinglirun"))},
                {"label": "营业利润", "value": yi(fin.get("yingyelirun"))},
                {"label": "利润总额", "value": yi(fin.get("lirunzonghe"))},
                {"label": "净利润", "value": yi(fin.get("jinglirun"))},
                {"label": "投资收益", "value": yi(fin.get("touzishouyu"))},
            ],
        },
        {
            "title": "资产负债",
            "rows": [
                {"label": "总资产", "value": yi(fin.get("zongzichan"))},
                {"label": "净资产", "value": yi(fin.get("jingzichan"))},
                {"label": "流动资产", "value": yi(fin.get("liudongzichan"))},
                {"label": "固定资产", "value": yi(fin.get("gudingzichan"))},
                {"label": "无形资产", "value": yi(fin.get("wuxingzichan"))},
                {"label": "流动负债", "value": yi(fin.get("liudongfuzhai"))},
                {"label": "长期负债", "value": yi(fin.get("changqifuzhai"))},
                {"label": "资本公积金", "value": yi(fin.get("zibengongjijin"))},
                {"label": "未分配利润", "value": yi(fin.get("weifenpeilirun"))},
            ],
        },
        {
            "title": "现金流量",
            "rows": [
                {"label": "经营现金流", "value": yi(fin.get("jingyingxianjinliu"))},
                {"label": "总现金流", "value": yi(fin.get("zongxianjinliu"))},
                {"label": "应收账款", "value": yi(fin.get("yingshouzhangkuan"))},
                {"label": "存货", "value": yi(fin.get("cunhuo"))},
            ],
        },
    ]

    # 派生财务比率
    zhuyingshouru = _num(fin.get("zhuyingshouru"))
    jinglirun = _num(fin.get("jinglirun"))
    zongzichan = _num(fin.get("zongzichan"))
    jingzichan = _num(fin.get("jingzichan"))

    extra: list[dict] = []
    if zhuyingshouru > 0 and jinglirun > 0:
        extra.append({"label": "净利率", "value": f"{jinglirun / zhuyingshouru * 100:.2f}%"})
    if jinglirun > 0 and jingzichan > 0:
        extra.append({"label": "ROE", "value": f"{jinglirun / jingzichan * 100:.2f}%"})
    if zongzichan > 0 and jingzichan > 0:
        extra.append({"label": "资产负债率", "value": f"{(zongzichan - jingzichan) / zongzichan * 100:.2f}%"})

    if extra:
        sections.append({"title": "财务比率", "rows": extra})

    return sections


def _synthetic_minute_points(price: float, change: float) -> list[dict]:
    base = price - change
    minimum = min(MINUTE_VALUES)
    maximum = max(MINUTE_VALUES)
    value_range = maximum - minimum or 1
    points = []
    for index, value in enumerate(MINUTE_VALUES):
        normalized = (value - minimum) / value_range - 0.5
        time_str = _format_minute_time_from_index(index, len(MINUTE_VALUES))
        points.append({"index": index, "time": time_str, "price": round(base + normalized * price * 0.018, 2)})
    return points


def _fetch_bars(code: str, frequency: int, offset: int) -> list[dict]:
    """按周期拉 K 线。走 bars slot。"""
    frame = mootdx_client.bars(code, frequency=frequency, offset=offset)
    if frame is None or frame.empty:
        return []
    bars = []
    for row in frame.to_dict("records"):
        open_price = _num(row.get("open"))
        close_price = _num(row.get("close"))
        high_price = _num(row.get("high"))
        low_price = _num(row.get("low"))
        if min(open_price, close_price, high_price, low_price) <= 0:
            continue
        bars.append(
            {
                "time": _format_kline_time(row.get("datetime") or row.get("date")),
                "open": round(open_price, 2),
                "high": round(high_price, 2),
                "low": round(low_price, 2),
                "close": round(close_price, 2),
                "volume": _num(row.get("vol") or row.get("volume")),
            }
        )
    return bars


def _aggregate_bars(bars: list[dict], group: int, period: str) -> list[dict]:
    aggregated: list[dict] = []
    for start in range(0, len(bars), group):
        chunk = bars[start : start + group]
        if not chunk:
            continue
        aggregated.append(
            {
                "time": chunk[0]["time"],
                "open": chunk[0]["open"],
                "close": chunk[-1]["close"],
                "high": max(item["high"] for item in chunk),
                "low": min(item["low"] for item in chunk),
                "volume": sum(item["volume"] for item in chunk),
            }
        )
    return aggregated


def _synthetic_kline(price: float, change: float, period: str) -> list[dict]:
    count, step_minutes, _label = _synthetic_kline_meta(period)
    base = price - change
    closes = []
    cur = base
    span = max(abs(change) * 1.2, price * 0.02)
    for i in range(count):
        progress = i / max(count - 1, 1)
        wave = math.sin(progress * math.pi * 2.6) * span * 0.35
        drift = progress * change
        cur = round(base + drift + wave, 2)
        closes.append(cur)
    bars = []
    for i, close_price in enumerate(closes):
        open_price = closes[i - 1] if i > 0 else base
        amplitude = abs(close_price - open_price) + price * 0.004
        high = round(max(open_price, close_price) + amplitude * 0.5, 2)
        low = round(min(open_price, close_price) - amplitude * 0.5, 2)
        volume = round(abs(close_price - open_price) * 10000 + price * 800 + i * 17, 0)
        bars.append(
            {
                "time": _synthetic_kline_time(period, count - 1 - i, step_minutes),
                "open": round(open_price, 2),
                "high": high,
                "low": low,
                "close": close_price,
                "volume": volume,
            }
        )
    bars.reverse()
    return bars


def _synthetic_kline_meta(period: str) -> tuple[int, int, str]:
    table = {
        "5m": (96, 5, "5分"),
        "15m": (96, 15, "15分"),
        "30m": (96, 30, "30分"),
        "60m": (120, 60, "60分"),
        "120m": (120, 120, "120分"),
        "day": (120, 24 * 60, "日K"),
        "week": (104, 7 * 24 * 60, "周K"),
        "mon": (36, 30 * 24 * 60, "月K"),
    }
    return table[period]


def _synthetic_kline_time(period: str, ago_index: int, step_minutes: int) -> str:
    now = time.localtime()
    if period in {"day", "week", "mon"}:
        base = time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d")
    else:
        base = now
    base_ts = time.mktime(base) - ago_index * step_minutes * 60
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(base_ts))


def _format_kline_time(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    if "T" in text:
        text = text.split("T", 0)[0] + " " + text.split("T", 1)[1]
    text = text.replace("-", ":").replace("/", ":").replace("T", " ")
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) >= 12:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]} {digits[8:10]}:{digits[10:12]}"
    if len(digits) >= 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return text


def _sort_items(items: list[dict], sort: str, order: str) -> list[dict]:
    reverse = (order or "desc").lower() != "asc"

    def key(item: dict):
        value = item.get(sort, "")
        if isinstance(value, (int, float)):
            return value
        return str(value)

    return sorted(items, key=key, reverse=reverse)


def _market_int(code: str) -> int:
    return 0 if code.startswith(("000", "001", "002", "003", "300", "301")) else 1


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


def _display_number(value: Any, fallback: float) -> float | None:
    """将字段转为可展示的数值。value 为 None 时返回 None（表示数据缺失）。"""
    if value is None:
        return None
    number = _num(value)
    return round(number if number > 0 else fallback, 2)


def _disp(value: Any, fallback: float) -> str | float:
    """等同于 _display_number，但 None 时返回 "--"。"""
    n = _display_number(value, fallback)
    return n if n is not None else "--"


def _metric_sub(value: Any, previous_close: float) -> str:
    number = _num(value)
    if number <= 0 or previous_close <= 0:
        return "--"
    pct = (number - previous_close) / previous_close * 100
    sign = "+" if pct >= 0 else ""
    return f"{sign}{pct:.2f}%"


def _market_cap_text(value: Any) -> str:
    number = _num(value)
    return f"{number:,.0f} 亿" if number > 0 else "--"


def _format_minute_time(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    if "T" in text:
        text = text.split("T", 1)[1]
    text = text.replace("-", ":").replace("/", ":")
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) >= 12:
        return f"{digits[8:10]}:{digits[10:12]}"
    if len(digits) >= 4:
        return f"{digits[:2]}:{digits[2:4]}"
    return text


def _format_minute_time_from_index(index: int, total: int) -> str:
    """A股交易时间: 09:30-11:30 (120分钟), 13:00-15:00 (120分钟)"""
    minutes_per_point = 240 / max(total - 1, 1)
    elapsed = int(index * minutes_per_point)
    if elapsed < 120:
        # 上午场 09:30 起
        total_minutes = 9 * 60 + 30 + elapsed
    else:
        # 下午场 13:00 起
        total_minutes = 13 * 60 + (elapsed - 120)
    hour = total_minutes // 60
    minute = total_minutes % 60
    return f"{hour:02d}:{minute:02d}"
