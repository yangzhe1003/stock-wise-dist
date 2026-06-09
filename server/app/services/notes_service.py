"""复盘笔记服务 — SQLite 持久化。"""

from __future__ import annotations

from app.core.sqlite_storage import get_storage


def get_notes(
    trade_date: str | None = None,
    tag: str | None = None,
    keyword: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> dict:
    """获取笔记列表（分页）。"""
    storage = get_storage()
    notes, total = storage.get_notes(
        trade_date=trade_date,
        tag=tag,
        keyword=keyword,
        page=page,
        page_size=page_size,
    )
    return {
        "items": notes,
        "total": total,
        "page": page,
        "pageSize": page_size,
        "totalPages": max(1, (total + page_size - 1) // page_size),
    }


def get_note(note_id: int) -> dict | None:
    """获取单篇笔记。"""
    return get_storage().get_note(note_id)


def create_note(
    title: str,
    trade_date: str,
    market_obs: str = "",
    trade_review: str = "",
    next_plan: str = "",
    tags: list[str] | None = None,
    stocks: list[dict[str, str]] | None = None,
) -> dict | None:
    """创建复盘笔记。返回完整笔记对象。"""
    note_id = get_storage().create_note(
        title=title,
        trade_date=trade_date,
        market_obs=market_obs,
        trade_review=trade_review,
        next_plan=next_plan,
        tags=tags,
        stock_codes=stocks,
    )
    if note_id is None:
        return None
    return get_note(note_id)


def update_note(
    note_id: int,
    title: str | None = None,
    market_obs: str | None = None,
    trade_review: str | None = None,
    next_plan: str | None = None,
    tags: list[str] | None = None,
    trade_date: str | None = None,
    stocks: list[dict[str, str]] | None = None,
) -> dict | None:
    """更新复盘笔记。只更新传入的非 None 字段。返回更新后的笔记。"""
    ok = get_storage().update_note(
        note_id=note_id,
        title=title,
        market_obs=market_obs,
        trade_review=trade_review,
        next_plan=next_plan,
        tags=tags,
        trade_date=trade_date,
        stock_codes=stocks,
    )
    if not ok:
        return None
    return get_note(note_id)


def delete_note(note_id: int) -> bool:
    """删除复盘笔记。"""
    return get_storage().delete_note(note_id)


def get_stats() -> dict:
    """获取复盘统计。"""
    return get_storage().get_notes_stats()


def get_calendar_dates(year: int, month: int) -> list[str]:
    """获取某月有笔记的日期列表。"""
    return get_storage().get_note_dates_for_month(year, month)


def export_notes(fmt: str = "json") -> str:
    """导出全部笔记为 JSON 或 Markdown 格式。"""
    storage = get_storage()
    notes, _ = storage.get_notes(page=1, page_size=10000)

    if fmt == "md":
        lines: list[str] = ["# 复盘笔记导出", ""]
        for note in notes:
            lines.append(f"## {note['tradeDate']} — {note['title']}")
            lines.append("")
            if note.get("tags"):
                lines.append(f"标签: {'、'.join(note['tags'])}")
                lines.append("")
            if note.get("linkedStocks"):
                stocks_str = "、".join(
                    f"{s['name']}({s['code']})" for s in note["linkedStocks"]
                )
                lines.append(f"关联标的: {stocks_str}")
                lines.append("")
            if note.get("marketObs"):
                lines.append("### 市场观察")
                lines.append("")
                lines.append(note["marketObs"])
                lines.append("")
            if note.get("tradeReview"):
                lines.append("### 操作复盘")
                lines.append("")
                lines.append(note["tradeReview"])
                lines.append("")
            if note.get("nextPlan"):
                lines.append("### 明日计划")
                lines.append("")
                lines.append(note["nextPlan"])
                lines.append("")
            lines.append("---")
            lines.append("")
        return "\n".join(lines)

    # JSON
    import json
    return json.dumps(notes, ensure_ascii=False, indent=2)


def get_notes_by_stock(stock_code: str, page: int = 1, page_size: int = 20) -> dict:
    """按标的反查关联笔记。"""
    storage = get_storage()
    notes, total = storage.get_notes_by_stock(
        stock_code=stock_code, page=page, page_size=page_size
    )
    return {
        "items": notes,
        "total": total,
        "page": page,
        "pageSize": page_size,
        "totalPages": max(1, (total + page_size - 1) // page_size),
    }


def generate_ai_draft(trade_date: str) -> dict:
    """根据当日行情、策略信号、自选股表现生成复盘草稿。

    返回 dict:
      - title: 建议标题
      - marketObs: 市场观察草稿
      - tradeReview: 操作复盘草稿
      - nextPlan: 明日计划草稿
      - tags: 建议标签
      - stocks: 建议关联标的
      - source: 数据来源说明
    """
    from app.services.market_service import get_market_overview
    from app.services.watchlist_service import get_watchlist
    from app.core.sqlite_storage import get_storage

    storage = get_storage()

    # ---- 1. 市场概况 ----
    market_lines: list[str] = []
    source_parts: list[str] = []

    try:
        overview = get_market_overview()
        source_parts.append("实时行情")

        # 指数表现
        indices = overview.get("indices", [])
        if indices:
            market_lines.append("## 指数表现")
            market_lines.append("")
            for idx in indices[:6]:
                name = idx.get("name", "")
                chg = idx.get("changePct", 0)
                direction = "↑" if chg > 0 else "↓" if chg < 0 else "→"
                market_lines.append(f"- {name}: {direction}{abs(chg):.2f}%")
            market_lines.append("")

        # 涨跌家数
        breadth = overview.get("breadth", {})
        up_count = breadth.get("up", 0)
        down_count = breadth.get("down", 0)
        flat_count = breadth.get("flat", 0)
        if up_count or down_count:
            market_lines.append("## 涨跌分布")
            market_lines.append("")
            market_lines.append(f"- 上涨 {up_count} 家 / 下跌 {down_count} 家 / 平盘 {flat_count} 家")
            if up_count + down_count > 0:
                ratio = round(up_count / (up_count + down_count) * 100, 1)
                market_lines.append(f"- 涨跌比: {ratio}%")
            market_lines.append("")

        # 热门板块
        sectors = overview.get("sectors", [])
        if sectors:
            market_lines.append("## 板块表现")
            market_lines.append("")
            top_sectors = sorted(sectors, key=lambda x: x.get("changePct", 0), reverse=True)[:5]
            for sec in top_sectors:
                market_lines.append(f"- {sec['name']}: {sec.get('changePct', 0):+.2f}%")
            bottom_sectors = sorted(sectors, key=lambda x: x.get("changePct", 0))[:3]
            if bottom_sectors:
                market_lines.append(f"- 跌幅居前: {'、'.join(s['name'] for s in bottom_sectors)}")
            market_lines.append("")
    except Exception:
        market_lines.append("（行情数据暂不可用）")
        market_lines.append("")

    # ---- 2. 策略信号 ----
    trade_lines: list[str] = []
    suggested_stocks: list[dict[str, str]] = []
    suggested_tags: list[str] = []

    try:
        # 获取最新策略结果
        rows = storage.conn.execute(
            "SELECT DISTINCT scan_id, strategy FROM strategy_results "
            "WHERE trade_date = ? ORDER BY scan_id DESC",
            (trade_date,),
        ).fetchall()

        if rows:
            source_parts.append("策略信号")
            trade_lines.append("## 策略信号")
            trade_lines.append("")

            for r in rows[:3]:
                strategy_name = {"s1": "趋势动量", "s2": "价值反转", "s3": "多因子精选"}.get(
                    r["strategy"], r["strategy"]
                )
                results = storage.get_strategy_results(r["scan_id"], r["strategy"])
                if results:
                    top3 = results[:3]
                    names = [f"{item['name']}({item['name']}, 得分{item['score']:.0f})" for item in top3]
                    trade_lines.append(f"- **{strategy_name}**: {', '.join(names)}")
                    for item in top3:
                        if item["code"] not in [s["code"] for s in suggested_stocks]:
                            suggested_stocks.append({"code": item["code"], "name": item["name"]})
            trade_lines.append("")
            suggested_tags.append("策略")
        else:
            trade_lines.append("（当日暂无策略扫描结果）")
            trade_lines.append("")
    except Exception:
        trade_lines.append("（策略数据暂不可用）")
        trade_lines.append("")

    # ---- 3. 自选股表现 ----
    try:
        watchlist = get_watchlist()
        if watchlist:
            source_parts.append("自选股")
            top_gainers = sorted(watchlist, key=lambda x: x.get("changePct", 0), reverse=True)[:5]
            top_losers = sorted(watchlist, key=lambda x: x.get("changePct", 0))[:3]

            trade_lines.append("## 自选股表现")
            trade_lines.append("")
            trade_lines.append("**涨幅居前:**")
            for s in top_gainers:
                trade_lines.append(f"- {s['name']}: {s.get('changePct', 0):+.2f}%")
            trade_lines.append("")
            trade_lines.append("**跌幅居前:**")
            for s in top_losers:
                trade_lines.append(f"- {s['name']}: {s.get('changePct', 0):+.2f}%")
            trade_lines.append("")

            # 自动关联自选股（最多5只）
            for s in watchlist[:5]:
                if s["code"] not in [st["code"] for st in suggested_stocks]:
                    suggested_stocks.append({"code": s["code"], "name": s["name"]})
    except Exception:
        pass

    # ---- 4. 拼接草稿 ----
    source = " + ".join(source_parts) if source_parts else "历史数据"

    # 生成标题
    title = f"复盘笔记 — {trade_date}"

    market_obs = "\n".join(market_lines) if market_lines else "（请手动填写市场观察）"

    trade_review_lines: list[str] = []
    trade_review_lines.append("## 操作记录")
    trade_review_lines.append("")
    trade_review_lines.append("（请在此记录今日实际操作）")
    trade_review_lines.append("")
    trade_review_lines.extend(trade_lines)
    trade_review = "\n".join(trade_review_lines)

    plan_lines: list[str] = [
        "## 关注方向",
        "",
        "（根据今日策略信号和市场表现生成关注方向）",
        "",
        "## 交易计划",
        "",
        "- 标的: ",
        "- 方向: ",
        "- 仓位: ",
        "- 止损: ",
        "",
        "## 风控提醒",
        "",
        "- 单票仓位不超过 ",
        "- 总仓位控制在 ",
        "- 关注外围市场风险",
    ]
    next_plan = "\n".join(plan_lines)

    return {
        "title": title,
        "marketObs": market_obs,
        "tradeReview": trade_review,
        "nextPlan": next_plan,
        "tags": suggested_tags,
        "stocks": suggested_stocks,
        "source": f"数据来源: {source}",
    }
