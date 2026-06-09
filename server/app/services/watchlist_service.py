"""自选股服务 — SQLite 持久化。

为避免首屏在缓存陈旧时同步拉取每只股票阻塞请求，对自选股行情拉取采用
stale-while-revalidate：先返回缓存快照，再后台异步批量刷行情。
"""

from __future__ import annotations

import threading

from app.core.sqlite_storage import get_storage
from app.services.mootdx_client import client as mootdx_client
from app.services.stock_service import get_stock_snapshot

# 防止并发重复刷新同一批自选股
_watchlist_lock = threading.Lock()


def get_watchlist(sort: str = "default", category_id: int | None = None) -> list[dict]:
    """获取自选股列表，附带实时行情。SWR 模式：先返回缓存，必要时后台异步刷新。"""
    storage = get_storage()
    if category_id is not None:
        saved = storage.get_watchlist_by_category(category_id)
    else:
        saved = storage.get_watchlist()
    if not saved:
        return []

    # 一次批量查询所有自选股的行情缓存（消除 N+1 查询）
    all_codes = [item["code"] for item in saved]
    quote_map = storage.get_cached_quotes_batch(all_codes)

    items = []
    codes_to_refresh: list[str] = []
    for item in saved:
        cached = quote_map.get(item["code"])
        if cached:
            cached = dict(cached)
            cached.setdefault("market", item.get("market", "sh"))
            items.append({**cached, "createdAt": item["createdAt"], "categories": item.get("categories", [])})
        else:
            codes_to_refresh.append(item["code"])

    # 后台异步刷新（仅在无任务运行时）
    if codes_to_refresh:
        _schedule_watchlist_refresh(codes_to_refresh)

    if sort == "gain":
        return sorted(items, key=lambda x: x.get("changePct", 0), reverse=True)
    if sort == "loss":
        return sorted(items, key=lambda x: x.get("changePct", 0))
    return items


def _schedule_watchlist_refresh(codes: list[str]) -> None:
    """非阻塞批量刷新自选股行情。"""
    if not _watchlist_lock.acquire(blocking=False):
        return
    thread = threading.Thread(
        target=_background_refresh_watchlist,
        args=(codes,),
        daemon=True,
        name="watchlist-quote-refresh",
    )
    thread.start()


def _background_refresh_watchlist(codes: list[str]) -> None:
    """后台线程：批量从 mootdx 拉取自选股行情并写缓存。"""
    storage = get_storage()
    try:
        # 批量最多 80 只（与市场服务一致）
        for start in range(0, len(codes), 80):
            batch = codes[start:start + 80]
            try:
                # 走独立 quote slot
                frame = mootdx_client.quote(batch)
            except Exception as exc:
                print(f"[自选刷新] 批量拉取失败：{exc}")
                continue
            if frame is None or frame.empty:
                continue
            for row in frame.to_dict("records"):
                code = str(row.get("code", "")).strip()
                if not code or code not in batch:
                    continue
                try:
                    stock = get_stock_snapshot(code)
                except Exception:
                    continue
                # 写缓存供下次请求直接读
                storage.upsert_single_quote(stock)
    except Exception as exc:
        print(f"[自选刷新] 后台任务失败：{exc}")
    finally:
        _watchlist_lock.release()


def add_watchlist(code: str, category_ids: list[int] | None = None) -> list[dict]:
    """添加自选股，可指定分类。"""
    stock = get_stock_snapshot(code)
    get_storage().add_watchlist(code, stock["name"], stock["market"], category_ids)
    return get_watchlist()


def delete_watchlist(code: str) -> list[dict]:
    """删除自选股。"""
    get_storage().delete_watchlist(code)
    return get_watchlist()


# ---- 分类管理 ----


def get_categories() -> list[dict]:
    """获取所有自选分类。"""
    return get_storage().get_categories()


def create_category(name: str) -> dict | None:
    """新建分类，返回创建后的分类对象。"""
    storage = get_storage()
    cid = storage.create_category(name)
    if cid is None:
        return None
    rows = storage.conn.execute(
        "SELECT id, name, sort_order, created_at FROM watchlist_categories WHERE id = ?", (cid,)
    ).fetchall()
    if rows:
        r = rows[0]
        return {"id": r[0], "name": r[1], "sort_order": r[2], "created_at": r[3]}
    return None


def delete_category(category_id: int) -> bool:
    """删除分类。"""
    return get_storage().delete_category(category_id)


def rename_category(category_id: int, name: str) -> bool:
    """重命名分类。"""
    return get_storage().rename_category(category_id, name)


def update_watchlist_categories(code: str, category_ids: list[int]) -> list[dict]:
    """更新某自选股的分类关联（替换式）。"""
    get_storage().set_watchlist_categories(code, category_ids)
    return get_watchlist()
