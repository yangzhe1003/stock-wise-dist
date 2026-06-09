"""SQLite 持久化存储管理器。

统一管理所有数据表，替代散落的 JSON 文件缓存。
表结构定义见同目录下的 schema.sql。
"""

from __future__ import annotations

import atexit
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

# 数据库文件路径
DB_PATH = Path(__file__).resolve().parents[2] / "data" / "stockbench.db"


def _fmt_amount(amount: float) -> str:
    """格式化成交额显示。"""
    if amount >= 1e8:
        return f"{amount / 1e8:.1f}亿"
    if amount >= 1e4:
        return f"{amount / 1e4:.0f}万"
    return str(round(amount))

# 建表 DDL，从 schema.sql 加载
def _load_schema() -> str:
    schema_path = Path(__file__).resolve().parent / "schema.sql"
    if schema_path.exists():
        return schema_path.read_text(encoding="utf-8")
    return ""

_SCHEMA_SQL = _load_schema()

# 线程本地连接
_local = threading.local()


def get_db_path() -> str:
    return str(DB_PATH)


class SqliteStorage:
    """SQLite 存储管理器（线程安全）。"""

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or str(DB_PATH)
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_db()
        # 轻量级内存缓存
        self._kline_stats_cache: dict[str, Any] | None = None
        self._kline_stats_ts: float = 0.0

    # ------------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------------

    @property
    def conn(self) -> sqlite3.Connection:
        """获取当前线程的数据库连接。"""
        key = f"_conn_{self.db_path}"
        conn = getattr(_local, key, None)
        if conn is None:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            setattr(_local, key, conn)
        return conn

    def close(self):
        """关闭当前线程的数据库连接。"""
        key = f"_conn_{self.db_path}"
        conn = getattr(_local, key, None)
        if conn:
            try:
                conn.close()
            except Exception:
                pass
            try:
                delattr(_local, key)
            except AttributeError:
                pass

    def close_all(self):
        """关闭所有线程的数据库连接（进程退出前调用）。"""
        prefix = f"_conn_{self.db_path}"
        # threading.local 的 __dict__ 包含所有线程的属性（线程安全）
        for key in list(getattr(_local, "__dict__", {}).keys()):
            if key.startswith("_conn_"):
                conn = getattr(_local, key, None)
                if conn:
                    try:
                        conn.close()
                    except Exception:
                        pass
                    try:
                        delattr(_local, key)
                    except AttributeError:
                        pass

    def _init_db(self):
        """首次运行时建表（幂等）。"""
        if not _SCHEMA_SQL:
            return
        # executescript 需要在非 WAL 模式下执行，或直接使用
        # 这里用隔离连接执行建表，避免和主连接冲突
        init_conn = sqlite3.connect(self.db_path)
        try:
            init_conn.executescript(_SCHEMA_SQL)
            init_conn.commit()
        except Exception:
            pass
        finally:
            init_conn.close()

        # 清理孤儿引用：将 strategy_reviews 中指向已删除笔记的 note_id 置空
        try:
            self.conn.execute(
                "UPDATE strategy_reviews SET note_id = NULL "
                "WHERE note_id IS NOT NULL "
                "AND note_id NOT IN (SELECT id FROM review_notes)"
            )
            self.conn.commit()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 通用帮助方法
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_dict(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        return dict(zip(row.keys(), row))

    @staticmethod
    def _rows_to_list(rows: list[sqlite3.Row]) -> list[dict]:
        return [dict(zip(r.keys(), r)) for r in rows]

    # ==================================================================
    # 1. 股票池 (stock_universe)
    # ==================================================================

    def upsert_universe(self, items: list[dict[str, Any]]) -> int:
        """批量写入/更新股票池。返回写入行数。"""
        if not items:
            return 0
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        rows = [
            (
                item["code"],
                item.get("name", ""),
                item.get("market", "sh"),
                item.get("marketName", ""),
                item.get("industry", ""),
                now,
            )
            for item in items
        ]
        with self.conn:
            self.conn.executemany(
                "INSERT INTO stock_universe(code, name, market, market_name, industry, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(code) DO UPDATE SET name=excluded.name, market=excluded.market, "
                "market_name=excluded.market_name, industry=excluded.industry, updated_at=excluded.updated_at",
                rows,
            )
        return len(rows)

    def get_universe(self, market: str = "all") -> list[dict[str, Any]]:
        """获取股票池列表。"""
        if market == "all":
            rows = self.conn.execute(
                "SELECT code, name, market, market_name AS marketName, industry "
                "FROM stock_universe ORDER BY code"
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT code, name, market, market_name AS marketName, industry "
                "FROM stock_universe WHERE market = ? ORDER BY code",
                (market,),
            ).fetchall()
        return self._rows_to_list(rows)

    def get_universe_count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) as cnt FROM stock_universe").fetchone()
        return row["cnt"] if row else 0

    def get_universe_codes(self) -> list[str]:
        rows = self.conn.execute("SELECT code FROM stock_universe ORDER BY code").fetchall()
        return [r["code"] for r in rows]

    def get_universe_status(self) -> dict[str, Any]:
        """获取股票池刷新状态。"""
        row = self.conn.execute("SELECT * FROM universe_refresh_status WHERE id = 1").fetchone()
        if not row:
            return {"count": 0, "cachedAt": 0, "updatedAt": "", "source": "none", "ageSeconds": None, "ttlSeconds": 86400, "fresh": False}
        status = self._row_to_dict(row)
        count = status.get("count", 0)
        cached_at = float(status.get("cached_at", 0))
        age = max(0, time.time() - cached_at) if cached_at else None
        fresh = count > 0 and age is not None and age < 86400
        return {
            "count": count,
            "updatedAt": status.get("updated_at", ""),
            "cachedAt": cached_at,
            "ageSeconds": age,
            "ttlSeconds": 86400,
            "fresh": fresh,
            "source": status.get("source", "none"),
        }

    def set_universe_status(self, count: int, source: str = "mootdx"):
        """更新股票池刷新状态。"""
        now_ts = time.time()
        now_str = time.strftime("%Y-%m-%d %H:%M:%S")
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO universe_refresh_status(id, count, cached_at, updated_at, source) "
                "VALUES (1, ?, ?, ?, ?)",
                (count, now_ts, now_str, source),
            )

    # ==================================================================
    # 2. 自选股 (watchlist)
    # ==================================================================

    def get_watchlist(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT code, name, market, created_at AS createdAt FROM watchlist ORDER BY created_at DESC"
        ).fetchall()
        items = self._rows_to_list(rows)
        # 附带每个股票的分类 ID 列表
        for item in items:
            item["categories"] = self._get_watchlist_categories_for_code(item["code"])
        return items

    def add_watchlist(self, code: str, name: str, market: str,
                       category_ids: list[int] | None = None) -> bool:
        """添加自选股。已存在则忽略。支持指定分类。返回是否新增。"""
        try:
            with self.conn:
                self.conn.execute(
                    "INSERT OR IGNORE INTO watchlist(code, name, market) VALUES (?, ?, ?)",
                    (code, name, market),
                )
                # 更新分类关联（无论新增还是已存在，同步分类）
                if category_ids:
                    for cid in category_ids:
                        self.conn.execute(
                            "INSERT OR IGNORE INTO watchlist_category_map(code, category_id) VALUES (?, ?)",
                            (code, cid),
                        )
            return True
        except Exception:
            return False

    def delete_watchlist(self, code: str) -> bool:
        """删除自选股。"""
        with self.conn:
            cur = self.conn.execute("DELETE FROM watchlist WHERE code = ?", (code,))
        return cur.rowcount > 0

    def is_watched(self, code: str) -> bool:
        row = self.conn.execute("SELECT 1 FROM watchlist WHERE code = ?", (code,)).fetchone()
        return row is not None

    # ==================================================================
    # 2a. 自选分类 (watchlist_categories)
    # ==================================================================

    def get_categories(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT id, name, sort_order, created_at FROM watchlist_categories ORDER BY sort_order, id"
        ).fetchall()
        return self._rows_to_list(rows)

    def create_category(self, name: str) -> int | None:
        """新建分类，返回 id。"""
        try:
            with self.conn:
                cur = self.conn.execute(
                    "INSERT INTO watchlist_categories(name, sort_order) VALUES (?, (SELECT COALESCE(MAX(sort_order),0)+1 FROM watchlist_categories))",
                    (name,),
                )
                return cur.lastrowid
        except Exception:
            return None

    def delete_category(self, category_id: int) -> bool:
        """删除分类（级联删除关联）。"""
        with self.conn:
            cur = self.conn.execute("DELETE FROM watchlist_categories WHERE id = ?", (category_id,))
        return cur.rowcount > 0

    def rename_category(self, category_id: int, name: str) -> bool:
        """重命名分类。"""
        with self.conn:
            cur = self.conn.execute(
                "UPDATE watchlist_categories SET name = ? WHERE id = ?", (name, category_id)
            )
        return cur.rowcount > 0

    # ==================================================================
    # 2b. 自选股-分类关联 (watchlist_category_map)
    # ==================================================================

    def _get_watchlist_categories_for_code(self, code: str) -> list[int]:
        """获取某股票所属分类 ID 列表。"""
        rows = self.conn.execute(
            "SELECT category_id FROM watchlist_category_map WHERE code = ?", (code,)
        ).fetchall()
        return [r[0] for r in rows]

    def set_watchlist_categories(self, code: str, category_ids: list[int]) -> bool:
        """替换某股票的分类关联（先删后插）。"""
        try:
            with self.conn:
                self.conn.execute(
                    "DELETE FROM watchlist_category_map WHERE code = ?", (code,)
                )
                for cid in category_ids:
                    self.conn.execute(
                        "INSERT OR IGNORE INTO watchlist_category_map(code, category_id) VALUES (?, ?)",
                        (code, cid),
                    )
            return True
        except Exception:
            return False

    def get_watchlist_by_category(self, category_id: int) -> list[dict[str, Any]]:
        """按分类筛选自选股。"""
        rows = self.conn.execute(
            """SELECT w.code, w.name, w.market, w.created_at AS createdAt
               FROM watchlist w
               JOIN watchlist_category_map m ON w.code = m.code
               WHERE m.category_id = ?
               ORDER BY w.created_at DESC""",
            (category_id,),
        ).fetchall()
        items = self._rows_to_list(rows)
        for item in items:
            item["categories"] = self._get_watchlist_categories_for_code(item["code"])
        return items

    # 默认自选股列表
    DEFAULT_WATCHLIST = [
        {"code": "600519", "name": "贵州茅台", "market": "sh"},
        {"code": "002594", "name": "比亚迪", "market": "sz"},
        {"code": "000858", "name": "五粮液", "market": "sz"},
        {"code": "300750", "name": "宁德时代", "market": "cyb"},
    ]

    def init_default_watchlist(self):
        """初始化默认自选股（首次运行）。"""
        existing = self.get_watchlist()
        if existing:
            return
        for item in self.DEFAULT_WATCHLIST:
            self.add_watchlist(item["code"], item["name"], item["market"])

    # ==================================================================
    # 3. 日K线缓存 (daily_kline)
    # ==================================================================

    def upsert_kline(self, code: str, bars: list[dict[str, Any]]) -> int:
        """批量写入K线数据（去重）。返回写入行数。"""
        if not bars:
            return 0
        rows = [
            (
                code,
                str(b.get("time") or b.get("trade_date", "")),
                float(b.get("open", 0) or 0),
                float(b.get("high", 0) or 0),
                float(b.get("low", 0) or 0),
                float(b.get("close", 0) or 0),
                float(b.get("volume", 0) or 0),
                float(b.get("amount", 0) or 0),
            )
            for b in bars
        ]
        with self.conn:
            self.conn.executemany(
                "INSERT OR IGNORE INTO daily_kline(code, trade_date, open, high, low, close, vol, amount) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        return len(rows)

    def get_kline(
        self,
        code: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[dict[str, Any]]:
        """获取K线数据，返回按日期升序的列表。"""
        query = "SELECT trade_date AS time, open, high, low, close, vol AS volume, amount FROM daily_kline WHERE code = ?"
        params: list[Any] = [code]
        if start_date:
            query += " AND trade_date >= ?"
            params.append(start_date)
        if end_date:
            query += " AND trade_date <= ?"
            params.append(end_date)
        query += " ORDER BY trade_date ASC"

        rows = self.conn.execute(query, params).fetchall()
        return self._rows_to_list(rows)

    def has_kline_data(self, code: str, start_date: str, end_date: str, min_rows: int = 60) -> bool:
        """检查是否有足够的K线数据。"""
        row = self.conn.execute(
            "SELECT COUNT(*) as cnt FROM daily_kline WHERE code = ? AND trade_date >= ? AND trade_date <= ?",
            (code, start_date, end_date),
        ).fetchone()
        return (row["cnt"] if row else 0) >= min_rows

    def max_kline_date(self, code: str) -> str | None:
        """获取某只股票的最新K线日期。"""
        row = self.conn.execute(
            "SELECT MAX(trade_date) FROM daily_kline WHERE code = ?", (code,)
        ).fetchone()
        return row[0] if row else None

    def get_stale_kline_codes(self, codes: list[str], min_date: str) -> list[str]:
        """返回 kline 最新日期早于 min_date 的股票代码列表。

        用于在分析前检查哪些股票的价格数据需要刷新。
        同时包含完全没有任何 kline 数据的股票。
        """
        if not codes:
            return []
        placeholders = ",".join(["?"] * len(codes))
        rows = self.conn.execute(
            f"SELECT code, MAX(trade_date) as latest FROM daily_kline "
            f"WHERE code IN ({placeholders}) GROUP BY code",
            codes,
        ).fetchall()
        stale: list[str] = []
        for row in rows:
            if not row["latest"] or row["latest"] < min_date:
                stale.append(row["code"])
        # 包含完全没有任何 kline 数据的 code
        existing = {row["code"] for row in rows}
        for code in codes:
            if code not in existing:
                stale.append(code)
        return stale

    def kline_stats(self) -> dict[str, Any]:
        """K线缓存统计（5 分钟内存缓存，避免频繁全表 COUNT）。"""
        now = time.time()
        if self._kline_stats_cache and (now - self._kline_stats_ts) < 300:
            return self._kline_stats_cache
        stock_count = self.conn.execute(
            "SELECT COUNT(DISTINCT code) FROM daily_kline"
        ).fetchone()[0]
        row_count = self.conn.execute("SELECT COUNT(*) FROM daily_kline").fetchone()[0]
        latest = self.conn.execute("SELECT MAX(trade_date) FROM daily_kline").fetchone()[0] or "-"
        earliest = self.conn.execute("SELECT MIN(trade_date) FROM daily_kline").fetchone()[0] or "-"
        result = {
            "stocks": stock_count,
            "rows": row_count,
            "date_range": f"{earliest} ~ {latest}",
        }
        self._kline_stats_cache = result
        self._kline_stats_ts = now
        return result

    # ==================================================================
    # 4. 实时行情快照 (stock_quotes_cache)
    # ==================================================================

    def upsert_quotes(self, items: list[dict[str, Any]]) -> int:
        """批量写入行情快照。"""
        if not items:
            return 0
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        rows = [
            (
                item["code"],
                item.get("name", ""),
                float(item.get("price", 0) or 0),
                float(item.get("change", 0) or 0),
                float(item.get("changePct", 0) or 0),
                float(item.get("open", 0) or 0),
                float(item.get("high", 0) or 0),
                float(item.get("low", 0) or 0),
                float(item.get("volume", 0) or 0),
                float(item.get("amount", 0) or 0),
                float(item.get("marketCap", 0) or 0),
                item.get("industry", ""),
                now,
            )
            for item in items
        ]
        with self.conn:
            self.conn.executemany(
                "INSERT OR REPLACE INTO stock_quotes_cache(code, name, price, change, change_pct, "
                "open, high, low, volume, amount, market_cap, industry, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        return len(rows)

    def get_quotes(self) -> list[dict[str, Any]]:
        """获取所有行情快照。"""
        rows = self.conn.execute(
            "SELECT code, name, price, change AS change, change_pct AS changePct, "
            "open, high, low, volume, amount, market_cap AS marketCap, industry, updated_at AS updatedAt "
            "FROM stock_quotes_cache ORDER BY code"
        ).fetchall()
        return self._rows_to_list(rows)

    def get_quote(self, code: str) -> dict[str, Any] | None:
        """获取单只股票行情快照。"""
        row = self.conn.execute(
            "SELECT code, name, price, change AS change, change_pct AS changePct, "
            "open, high, low, volume, amount, market_cap AS marketCap, industry "
            "FROM stock_quotes_cache WHERE code = ?",
            (code,),
        ).fetchone()
        return self._row_to_dict(row)

    def get_quote_lookup(self) -> dict[str, dict[str, Any]]:
        """获取行情快照的 code → {price, change, changePct, volume, marketCap, industry} 查找表。

        跳过价格无效（≤0）的缓存条目。
        """
        rows = self.conn.execute(
            "SELECT code, price, change, change_pct, volume, market_cap, industry "
            "FROM stock_quotes_cache WHERE price > 0"
        ).fetchall()
        return {
            r["code"]: {
                "price": r["price"],
                "change": r["change"],
                "changePct": r["change_pct"],
                "volume": r["volume"],
                "marketCap": r["market_cap"],
                "industry": r["industry"],
            }
            for r in rows
        }

    def get_cached_quote(self, code: str) -> dict[str, Any] | None:
        """读取单只股票的行情缓存。返回完整 stock 字典或 None。

        价格无效（≤0）时视为缓存未命中，强制重新拉取。
        """
        row = self.conn.execute(
            "SELECT code, name, price, change, change_pct, open, high, low, "
            "volume, amount, market_cap, industry, updated_at "
            "FROM stock_quotes_cache WHERE code = ?", (code,)
        ).fetchone()
        if not row:
            return None
        price = row["price"]
        if price is None or price <= 0:
            return None  # 无效数据，视为未命中
        return self._row_to_cached_quote(row)

    def get_cached_quotes_batch(self, codes: list[str]) -> dict[str, dict[str, Any]]:
        """批量读取行情缓存。一次 SQL 查询，返回 code → stock 映射。

        替代对 get_cached_quote 的 N 次独立调用，消除自选列表的 N+1 查询。
        价格无效（≤0）的条目不会出现在返回结果中。
        """
        if not codes:
            return {}
        placeholders = ",".join(["?"] * len(codes))
        rows = self.conn.execute(
            "SELECT code, name, price, change, change_pct, open, high, low, "
            "volume, amount, market_cap, industry, updated_at "
            f"FROM stock_quotes_cache WHERE code IN ({placeholders})",
            codes,
        ).fetchall()
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            if row["price"] is None or row["price"] <= 0:
                continue
            result[row["code"]] = self._row_to_cached_quote(row)
        return result

    @staticmethod
    def _row_to_cached_quote(row: sqlite3.Row) -> dict[str, Any]:
        """将数据库行转为 stock 字典。"""
        return {
            "code": row["code"],
            "name": row["name"],
            "price": row["price"],
            "change": row["change"],
            "changePct": row["change_pct"],
            "volume": row["volume"],
            "open": row["open"] if row["open"] > 0 else None,
            "high": row["high"] if row["high"] > 0 else None,
            "low": row["low"] if row["low"] > 0 else None,
            "amount": row["amount"],
            "amountText": _fmt_amount(row["amount"]),
            "marketCap": row["market_cap"],
            "industry": row["industry"] or "--",
            "updatedAt": row["updated_at"],
        }

    def upsert_single_quote(self, stock: dict[str, Any]) -> bool:
        """写入/更新单只股票行情缓存。价格无效（≤0）时跳过写入。"""
        try:
            price = float(stock.get("price", 0) or 0)
            if price <= 0:
                return False  # 无效价格不缓存，避免污染缓存导致后续一直读到 0
            now = time.strftime("%Y-%m-%d %H:%M:%S")
            with self.conn:
                self.conn.execute(
                    "INSERT OR REPLACE INTO stock_quotes_cache "
                    "(code, name, price, change, change_pct, open, high, low, volume, amount, market_cap, industry, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        stock["code"],
                        stock.get("name", ""),
                        price,
                        float(stock.get("change", 0) or 0),
                        float(stock.get("changePct", 0) or 0),
                        float(stock.get("open", 0) or 0) if stock.get("open") is not None else 0,
                        float(stock.get("high", 0) or 0) if stock.get("high") is not None else 0,
                        float(stock.get("low", 0) or 0) if stock.get("low") is not None else 0,
                        float(stock.get("volume", 0) or 0),
                        float(stock.get("amount", 0) or 0),
                        float(stock.get("marketCap", 0) or 0),
                        stock.get("industry", ""),
                        now,
                    ),
                )
            return True
        except Exception:
            return False

    def is_quotes_fresh(self) -> bool:
        """检查行情缓存是否来自最近一次收盘时刻。

        新规则（非交易时间缓存判据）：
        - 缓存里 ``updated_at`` 最新的那条记录 >= 最近一次 15:00 收盘 → True
        - 否则 → False（应重新拉取）
        """
        from app.services.market_calendar import last_close_time
        row = self.conn.execute(
            "SELECT MAX(updated_at) AS latest FROM stock_quotes_cache"
        ).fetchone()
        if not row or not row["latest"]:
            return False
        try:
            latest = time.mktime(time.strptime(row["latest"], "%Y-%m-%d %H:%M:%S"))
            return latest >= last_close_time()
        except (ValueError, OSError):
            return False

    def is_quote_fresh(self, code: str) -> bool:
        """检查某只股票的缓存是否来自最近一次收盘时刻。

        新规则（详情页缓存判据）：``updated_at >= 最近一次 15:00 收盘``。
        盘中 ``_is_trading_now()`` 会先 short-circuit 走 fresh 路径，
        所以这里只在非交易时间被调用。
        """
        from app.services.market_calendar import last_close_time
        row = self.conn.execute(
            "SELECT updated_at, price FROM stock_quotes_cache WHERE code = ?", (code,)
        ).fetchone()
        if not row or not row["updated_at"]:
            return False
        if row["price"] is None or float(row["price"]) <= 0:
            return False
        try:
            updated = time.mktime(time.strptime(row["updated_at"], "%Y-%m-%d %H:%M:%S"))
            return updated >= last_close_time()
        except (ValueError, OSError):
            return False

    # ==================================================================
    # 5. 市场概况缓存 (market_overview_cache)
    # ==================================================================

    def get_market_overview(self) -> dict[str, Any] | None:
        """获取缓存的市场概况。"""
        row = self.conn.execute("SELECT data_json, cached_at, source FROM market_overview_cache WHERE id = 1").fetchone()
        if not row:
            return None
        try:
            data = json.loads(row["data_json"])
            data["_cached_at"] = row["cached_at"]
            data["_source"] = row["source"]
            return data
        except json.JSONDecodeError:
            return None

    def set_market_overview(self, data: dict[str, Any], source: str = "mootdx"):
        """缓存市场概况。"""
        data_json = json.dumps(data, ensure_ascii=False)
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO market_overview_cache(id, data_json, cached_at, source) "
                "VALUES (1, ?, ?, ?)",
                (data_json, time.time(), source),
            )

    def is_market_overview_fresh(self) -> bool:
        """检查市场概况缓存是否来自最近一次收盘时刻。

        新规则（非交易时间缓存判据）：``cached_at >= 最近一次 15:00 收盘``。
        盘中 ``_is_trading_now()`` 会先 short-circuit 走 fresh 路径，所以这里
        只在非交易时间被调用。
        """
        row = self.conn.execute("SELECT cached_at FROM market_overview_cache WHERE id = 1").fetchone()
        if not row:
            return False
        from app.services.market_calendar import last_close_time
        return float(row["cached_at"]) >= last_close_time()

    # ==================================================================
    # 5a. 市场总览刷新状态 (market_overview_status)
    #     单行表，追踪后台刷新任务。前端通过 /api/market/overview/status 轮询。
    #     若 refreshing=1 持续超过 STALE_LOCK_SECONDS（防崩溃遗留），自动清除。
    # ==================================================================

    STALE_LOCK_SECONDS = 300  # 5 分钟未完成视为遗留锁

    def set_market_refreshing(self, refreshing: bool, error: str = "") -> None:
        """设置/清除后台刷新状态。

        refreshing=True  → 写入 started_at=now，error 清空
        refreshing=False → 保留 last_success / last_error 字段，started_at 置 0
        """
        now = time.time() if refreshing else 0.0
        with self.conn:
            if refreshing:
                self.conn.execute(
                    "INSERT INTO market_overview_status(id, refreshing, started_at, last_error) "
                    "VALUES (1, 1, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET refreshing=1, started_at=excluded.started_at, "
                    "last_error=''",
                    (now, ""),
                )
            else:
                self.conn.execute(
                    "INSERT INTO market_overview_status(id, refreshing, started_at, last_error) "
                    "VALUES (1, 0, 0, ?) "
                    "ON CONFLICT(id) DO UPDATE SET refreshing=0, started_at=0, "
                    "last_error=excluded.last_error",
                    (error,),
                )

    def get_market_overview_status(self) -> dict[str, Any]:
        """读取刷新状态，自动清理超过 STALE_LOCK_SECONDS 的遗留锁。"""
        row = self.conn.execute(
            "SELECT refreshing, started_at, last_success, last_error "
            "FROM market_overview_status WHERE id = 1"
        ).fetchone()
        if not row:
            return {
                "refreshing": False,
                "started_at": 0.0,
                "last_success": 0.0,
                "last_error": "",
                "age_seconds": None,
            }
        refreshing = bool(row["refreshing"])
        started_at = float(row["started_at"] or 0)
        last_success = float(row["last_success"] or 0)
        last_error = row["last_error"] or ""

        # 防御性：崩溃遗留锁自动清除
        if refreshing and started_at and (time.time() - started_at) > self.STALE_LOCK_SECONDS:
            self.conn.execute(
                "UPDATE market_overview_status SET refreshing=0, started_at=0 WHERE id = 1"
            )
            self.conn.commit()
            refreshing = False
            started_at = 0.0

        age = (time.time() - started_at) if (refreshing and started_at) else None
        return {
            "refreshing": refreshing,
            "started_at": started_at,
            "last_success": last_success,
            "last_error": last_error,
            "age_seconds": age,
        }

    def record_overview_success(self) -> None:
        """记录本次刷新成功时间。"""
        with self.conn:
            self.conn.execute(
                "INSERT INTO market_overview_status(id, refreshing, started_at, last_success, last_error) "
                "VALUES (1, 0, 0, ?, '') "
                "ON CONFLICT(id) DO UPDATE SET last_success=excluded.last_success, last_error=''",
                (time.time(),),
            )

    # ==================================================================
    # 6. 策略筛选结果 (strategy_results)
    # ==================================================================

    def save_strategy_results(self, scan_id: str, strategy: str, trade_date: str, results: list[dict[str, Any]]) -> int:
        """批量保存策略筛选结果。"""
        if not results:
            return 0
        rows = [
            (
                scan_id,
                strategy,
                r["code"],
                r.get("name", ""),
                float(r.get("score", 0)),
                int(r.get("rank", i + 1)),
                json.dumps(r.get("factors_detail", {}), ensure_ascii=False),
                json.dumps(r.get("signals", []), ensure_ascii=False),
                json.dumps(r.get("metrics", {}), ensure_ascii=False),
                trade_date,
            )
            for i, r in enumerate(results)
        ]
        with self.conn:
            self.conn.executemany(
                "INSERT OR IGNORE INTO strategy_results(scan_id, strategy, code, name, score, rank, "
                "factors_json, signals_json, metrics_json, trade_date) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        return len(rows)

    def get_strategy_results(self, scan_id: str, strategy: str) -> list[dict[str, Any]]:
        """获取指定扫描的策略结果。"""
        rows = self.conn.execute(
            "SELECT code, name, score, rank, factors_json, signals_json, metrics_json "
            "FROM strategy_results WHERE scan_id = ? AND strategy = ? ORDER BY rank",
            (scan_id, strategy),
        ).fetchall()
        results = []
        for r in rows:
            d = self._row_to_dict(r)
            d["factors_detail"] = json.loads(d.pop("factors_json", "{}"))
            d["signals"] = json.loads(d.pop("signals_json", "[]"))
            d["metrics"] = json.loads(d.pop("metrics_json", "{}"))
            results.append(d)
        return results

    def get_latest_scan_id(self, strategy: str, trade_date: str) -> str | None:
        """获取指定策略和交易日的最新已完成扫描ID。"""
        row = self.conn.execute(
            "SELECT scan_id FROM strategy_scan_log "
            "WHERE strategy = ? AND trade_date = ? AND status = 'completed' "
            "ORDER BY scan_id DESC LIMIT 1",
            (strategy, trade_date),
        ).fetchone()
        return row["scan_id"] if row else None

    def get_consensus_stocks(self, scan_id: str, min_strategies: int = 2) -> list[dict[str, Any]]:
        """获取多策略共振的股票（同一交易日同时出现在多个策略中）。

        根据 scan_id 找到对应交易日，然后跨所有策略查询共振。
        """
        # 先查 scan 所属的 trade_date
        scan = self.get_scan_status(scan_id)
        if not scan:
            return []
        trade_date = scan["trade_date"]

        rows = self.conn.execute(
            "SELECT sr.code, sr.name, GROUP_CONCAT(sr.strategy) AS strategies, "
            "AVG(sr.score) AS avg_score, COUNT(DISTINCT sr.strategy) AS strategy_count "
            "FROM strategy_results sr "
            "INNER JOIN strategy_scan_log sl ON sr.scan_id = sl.scan_id "
            "WHERE sr.trade_date = ? AND sl.status = 'completed' "
            "GROUP BY sr.code, sr.name HAVING COUNT(DISTINCT sr.strategy) >= ? "
            "ORDER BY avg_score DESC",
            (trade_date, min_strategies),
        ).fetchall()
        results = []
        for r in rows:
            d = self._row_to_dict(r)
            d["strategies"] = d["strategies"].split(",") if d.get("strategies") else []
            results.append(d)
        return results

    # ==================================================================
    # 7. 扫描日志 (strategy_scan_log)
    # ==================================================================

    def start_scan(self, scan_id: str, strategy: str, trade_date: str, total_stocks: int) -> None:
        """记录扫描开始。"""
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO strategy_scan_log(scan_id, strategy, trade_date, status, total_stocks, started_at) "
                "VALUES (?, ?, ?, 'running', ?, datetime('now', '+8 hours'))",
                (scan_id, strategy, trade_date, total_stocks),
            )

    def complete_scan(self, scan_id: str, matched_count: int, duration_ms: int):
        """标记扫描完成。"""
        with self.conn:
            self.conn.execute(
                "UPDATE strategy_scan_log SET status = 'completed', matched_count = ?, "
                "duration_ms = ?, completed_at = datetime('now', '+8 hours') WHERE scan_id = ?",
                (matched_count, duration_ms, scan_id),
            )

    def fail_scan(self, scan_id: str, error: str):
        """标记扫描失败。"""
        with self.conn:
            self.conn.execute(
                "UPDATE strategy_scan_log SET status = 'failed', error_message = ?, "
                "completed_at = datetime('now', '+8 hours') WHERE scan_id = ?",
                (error, scan_id),
            )

    def get_scan_status(self, scan_id: str) -> dict[str, Any] | None:
        """获取扫描状态。"""
        row = self.conn.execute(
            "SELECT * FROM strategy_scan_log WHERE scan_id = ?", (scan_id,)
        ).fetchone()
        return self._row_to_dict(row)

    def get_latest_scans(self, limit: int = 10) -> list[dict[str, Any]]:
        """获取最近的扫描记录。"""
        rows = self.conn.execute(
            "SELECT * FROM strategy_scan_log ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return self._rows_to_list(rows)

    def get_strategy_scans(
        self, strategy: str, limit: int = 50, offset: int = 0
    ) -> list[dict[str, Any]]:
        """获取指定策略的历史扫描记录（按时间倒序）。"""
        rows = self.conn.execute(
            "SELECT * FROM strategy_scan_log WHERE strategy = ? "
            "ORDER BY started_at DESC LIMIT ? OFFSET ?",
            (strategy, limit, offset),
        ).fetchall()
        return self._rows_to_list(rows)

    def count_strategy_scans(self, strategy: str) -> int:
        """获取指定策略的扫描总次数。"""
        row = self.conn.execute(
            "SELECT COUNT(*) FROM strategy_scan_log WHERE strategy = ?",
            (strategy,),
        ).fetchone()
        return row[0] if row else 0

    def delete_scan(self, scan_id: str) -> bool:
        """删除指定扫描记录及其结果（级联删除）。"""
        with self.conn:
            self.conn.execute(
                "DELETE FROM strategy_results WHERE scan_id = ?", (scan_id,)
            )
            cur = self.conn.execute(
                "DELETE FROM strategy_scan_log WHERE scan_id = ?", (scan_id,)
            )
        return cur.rowcount > 0

    def get_scan_analysis(self, scan_id: str) -> dict[str, Any] | None:
        """获取策略扫描的绩效分析：各标的执行至今涨跌幅、统计概览、得分相关性。

        返回 None 表示 scan_id 不存在。
        返回 dict:
          - scan_id, strategy, trade_date
          - stocks: [{code, name, score, exec_price, latest_price, latest_date, return_pct}]
          - summary: {avg_return, median_return, win_rate, best, worst, total}
          - score_groups: [{range, label, count, avg_return, win_rate}]
        """
        scan = self.get_scan_status(scan_id)
        if not scan:
            return None

        strategy = scan["strategy"]
        trade_date = scan["trade_date"]

        # 获取策略结果，附带最新 K 线收盘价
        rows = self.conn.execute(
            "SELECT sr.code, sr.name, sr.score, sr.rank, sr.metrics_json, "
            "   (SELECT kl.close FROM daily_kline kl "
            "     WHERE kl.code = sr.code ORDER BY kl.trade_date DESC LIMIT 1) AS latest_close, "
            "   (SELECT kl.trade_date FROM daily_kline kl "
            "     WHERE kl.code = sr.code ORDER BY kl.trade_date DESC LIMIT 1) AS latest_date "
            "FROM strategy_results sr "
            "WHERE sr.scan_id = ? AND sr.strategy = ? "
            "ORDER BY sr.rank",
            (scan_id, strategy),
        ).fetchall()

        if not rows:
            return {
                "scan_id": scan_id,
                "strategy": strategy,
                "trade_date": trade_date,
                "stocks": [],
                "summary": None,
                "score_groups": [],
            }

        stocks = []
        returns = []
        for r in rows:
            d = self._row_to_dict(r)
            metrics = json.loads(d.pop("metrics_json", "{}"))
            exec_price = metrics.get("close", 0)
            latest_price = d.get("latest_close")
            latest_date = d.get("latest_date")

            ret_pct = None
            if exec_price and latest_price and exec_price > 0:
                ret_pct = round((latest_price - exec_price) / exec_price * 100, 2)

            stocks.append({
                "code": d["code"],
                "name": d["name"],
                "score": d["score"],
                "rank": d["rank"],
                "exec_price": exec_price,
                "latest_price": latest_price,
                "latest_date": latest_date,
                "return_pct": ret_pct,
            })
            if ret_pct is not None:
                returns.append(ret_pct)

        # ---- 统计概览 ----
        summary = None
        if returns:
            sorted_ret = sorted(returns)
            n = len(sorted_ret)
            avg_return = round(sum(returns) / n, 2)
            median_return = round(sorted_ret[n // 2], 2) if n > 0 else 0
            win_count = sum(1 for r in returns if r > 0)
            win_rate = round(win_count / n * 100, 1)
            best_idx = returns.index(max(returns))
            worst_idx = returns.index(min(returns))
            summary = {
                "avg_return": avg_return,
                "median_return": median_return,
                "win_rate": win_rate,
                "total": n,
                "best": {
                    "code": stocks[best_idx]["code"],
                    "name": stocks[best_idx]["name"],
                    "return_pct": max(returns),
                },
                "worst": {
                    "code": stocks[worst_idx]["code"],
                    "name": stocks[worst_idx]["name"],
                    "return_pct": min(returns),
                },
            }

        # ---- 得分相关性 ----
        score_groups: list[dict] = []
        buckets = [(80, 100, "80-100"), (60, 80, "60-79"), (40, 60, "40-59"), (0, 40, "0-39")]
        for lo, hi, label in buckets:
            group_stocks = [s for s in stocks if s["score"] is not None and lo <= s["score"] < (hi if hi < 100 else 101)]
            group_returns = [s["return_pct"] for s in group_stocks if s["return_pct"] is not None]
            score_groups.append({
                "range": label,
                "label": f"{lo}-{hi - 1}分" if hi < 100 else "80-100分",
                "count": len(group_stocks),
                "avg_return": round(sum(group_returns) / len(group_returns), 2) if group_returns else None,
                "win_rate": round(sum(1 for r in group_returns if r > 0) / len(group_returns) * 100, 1) if group_returns else None,
            })

        return {
            "scan_id": scan_id,
            "strategy": strategy,
            "trade_date": trade_date,
            "stocks": stocks,
            "summary": summary,
            "score_groups": score_groups,
        }

    # ==================================================================
    # 8. 复盘笔记 (review_notes)
    # ==================================================================

    def create_note(self, title: str, trade_date: str,
                    market_obs: str = "", trade_review: str = "",
                    next_plan: str = "", tags: list[str] | None = None,
                    stock_codes: list[dict[str, str]] | None = None) -> int | None:
        """创建复盘笔记。返回新笔记 id。"""
        tags_json = json.dumps(tags or [], ensure_ascii=False)
        word_count = len(market_obs) + len(trade_review) + len(next_plan)
        try:
            with self.conn:
                cur = self.conn.execute(
                    "INSERT INTO review_notes(title, market_obs, trade_review, next_plan, "
                    "tags_json, word_count, trade_date) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (title, market_obs, trade_review, next_plan, tags_json, word_count, trade_date),
                )
                note_id = cur.lastrowid
                if stock_codes:
                    for s in stock_codes:
                        self.conn.execute(
                            "INSERT OR IGNORE INTO review_note_stocks(note_id, stock_code, stock_name) "
                            "VALUES (?, ?, ?)",
                            (note_id, s["code"], s.get("name", "")),
                        )
            return note_id
        except Exception:
            return None

    def update_note(self, note_id: int, title: str | None = None,
                    market_obs: str | None = None, trade_review: str | None = None,
                    next_plan: str | None = None, tags: list[str] | None = None,
                    trade_date: str | None = None,
                    stock_codes: list[dict[str, str]] | None = None) -> bool:
        """更新复盘笔记。只更新传入的非 None 字段。"""
        fields: list[str] = []
        params: list[Any] = []

        if title is not None:
            fields.append("title = ?")
            params.append(title)
        if market_obs is not None:
            fields.append("market_obs = ?")
            params.append(market_obs)
        if trade_review is not None:
            fields.append("trade_review = ?")
            params.append(trade_review)
        if next_plan is not None:
            fields.append("next_plan = ?")
            params.append(next_plan)
        if tags is not None:
            fields.append("tags_json = ?")
            params.append(json.dumps(tags, ensure_ascii=False))
        if trade_date is not None:
            fields.append("trade_date = ?")
            params.append(trade_date)

        # 重新计算字数（如果内容字段有更新）
        if market_obs is not None or trade_review is not None or next_plan is not None:
            fields.append("word_count = ?")
            # 先查当前值
            current = self.get_note(note_id)
            if current:
                mo = market_obs if market_obs is not None else current.get("marketObs", "")
                tr = trade_review if trade_review is not None else current.get("tradeReview", "")
                np_ = next_plan if next_plan is not None else current.get("nextPlan", "")
                params.append(len(mo) + len(tr) + len(np_))
            else:
                params.append(0)

        if not fields:
            return False

        fields.append("updated_at = datetime('now', '+8 hours')")
        params.append(note_id)

        try:
            with self.conn:
                self.conn.execute(
                    f"UPDATE review_notes SET {', '.join(fields)} WHERE id = ?",
                    params,
                )
                if stock_codes is not None:
                    self.conn.execute(
                        "DELETE FROM review_note_stocks WHERE note_id = ?", (note_id,)
                    )
                    for s in stock_codes:
                        self.conn.execute(
                            "INSERT OR IGNORE INTO review_note_stocks(note_id, stock_code, stock_name) "
                            "VALUES (?, ?, ?)",
                            (note_id, s["code"], s.get("name", "")),
                        )
            return True
        except Exception:
            return False

    def delete_note(self, note_id: int) -> bool:
        """删除复盘笔记（级联删除关联标的，并置空策略复盘引用）。"""
        with self.conn:
            # 将引用该笔记的策略复盘记录的 note_id 置空
            self.conn.execute(
                "UPDATE strategy_reviews SET note_id = NULL WHERE note_id = ?",
                (note_id,),
            )
            cur = self.conn.execute("DELETE FROM review_notes WHERE id = ?", (note_id,))
        return cur.rowcount > 0

    def get_note(self, note_id: int) -> dict[str, Any] | None:
        """获取单篇笔记（含关联标的）。"""
        row = self.conn.execute(
            "SELECT id, title, market_obs, trade_review, next_plan, "
            "tags_json, word_count, trade_date, created_at, updated_at "
            "FROM review_notes WHERE id = ?",
            (note_id,),
        ).fetchone()
        if not row:
            return None
        note = self._row_to_dict(row)
        note["tags"] = json.loads(note.pop("tags_json", "[]"))
        note["marketObs"] = note.pop("market_obs", "")
        note["tradeReview"] = note.pop("trade_review", "")
        note["nextPlan"] = note.pop("next_plan", "")
        note["wordCount"] = note.pop("word_count", 0)
        note["tradeDate"] = note.pop("trade_date", "")
        note["createdAt"] = note.pop("created_at", "")
        note["updatedAt"] = note.pop("updated_at", "")
        note["linkedStocks"] = self._get_note_stocks(note_id)
        return note

    def get_notes(self, trade_date: str | None = None,
                  tag: str | None = None, keyword: str | None = None,
                  page: int = 1, page_size: int = 20) -> tuple[list[dict[str, Any]], int]:
        """获取笔记列表（分页）。返回 (笔记列表, 总数)。"""
        where_clauses: list[str] = []
        params: list[Any] = []

        if trade_date:
            where_clauses.append("trade_date = ?")
            params.append(trade_date)
        if tag:
            where_clauses.append("tags_json LIKE ?")
            params.append(f'%"{tag}"%')
        if keyword:
            where_clauses.append(
                "(title LIKE ? OR market_obs LIKE ? OR trade_review LIKE ? OR next_plan LIKE ?)"
            )
            kw = f"%{keyword}%"
            params.extend([kw, kw, kw, kw])

        where = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

        # 总数
        count_row = self.conn.execute(
            f"SELECT COUNT(*) FROM review_notes {where}", params
        ).fetchone()
        total = count_row[0] if count_row else 0

        # 分页查询
        offset = (page - 1) * page_size
        rows = self.conn.execute(
            f"SELECT id, title, market_obs, trade_review, next_plan, "
            f"tags_json, word_count, trade_date, created_at, updated_at "
            f"FROM review_notes {where} ORDER BY trade_date DESC, created_at DESC "
            f"LIMIT ? OFFSET ?",
            params + [page_size, offset],
        ).fetchall()

        notes = []
        for r in rows:
            note = self._row_to_dict(r)
            note["tags"] = json.loads(note.pop("tags_json", "[]"))
            note["marketObs"] = note.pop("market_obs", "")
            note["tradeReview"] = note.pop("trade_review", "")
            note["nextPlan"] = note.pop("next_plan", "")
            note["wordCount"] = note.pop("word_count", 0)
            note["tradeDate"] = note.pop("trade_date", "")
            note["createdAt"] = note.pop("created_at", "")
            note["updatedAt"] = note.pop("updated_at", "")
            notes.append(note)

        # 批量加载关联标的（一条 JOIN 替代 N 次独立查询）
        if notes:
            note_ids = [n["id"] for n in notes]
            stocks_map = self._get_note_stocks_batch(note_ids)
            for n in notes:
                n["linkedStocks"] = stocks_map.get(n["id"], [])

        return notes, total

    def get_note_dates_for_month(self, year: int, month: int) -> list[str]:
        """获取某月有笔记的日期列表（YYYY-MM-DD）。"""
        prefix = f"{year:04d}-{month:02d}"
        rows = self.conn.execute(
            "SELECT DISTINCT trade_date FROM review_notes "
            "WHERE trade_date LIKE ? ORDER BY trade_date",
            (f"{prefix}%",),
        ).fetchall()
        return [r["trade_date"] for r in rows]

    def get_notes_stats(self) -> dict[str, Any]:
        """获取复盘笔记统计信息。"""
        # 总篇数
        total = self.conn.execute("SELECT COUNT(*) FROM review_notes").fetchone()[0]

        # 本月篇数
        import datetime
        now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
        month_prefix = now.strftime("%Y-%m")
        monthly = self.conn.execute(
            "SELECT COUNT(*) FROM review_notes WHERE trade_date LIKE ?",
            (f"{month_prefix}%",),
        ).fetchone()[0]

        # 关联标的总数（去重）
        stocks_count = self.conn.execute(
            "SELECT COUNT(DISTINCT stock_code) FROM review_note_stocks"
        ).fetchone()[0]

        # 标签总数（从 JSON 中统计，这里用近似：所有笔记的标签计数）
        tag_counter: dict[str, int] = {}
        rows = self.conn.execute(
            "SELECT tags_json FROM review_notes"
        ).fetchall()
        for r in rows:
            try:
                tags = json.loads(r["tags_json"])
                for t in tags:
                    tag_counter[t] = tag_counter.get(t, 0) + 1
            except (json.JSONDecodeError, TypeError):
                pass

        # 高频标签 top 10
        top_tags = sorted(tag_counter.items(), key=lambda x: x[1], reverse=True)[:10]

        # 连续记录天数
        streak = self._calculate_streak()

        return {
            "total": total,
            "monthly": monthly,
            "streak": streak,
            "linkedStocks": stocks_count,
            "tagCount": len(tag_counter),
            "topTags": [{"name": name, "count": count} for name, count in top_tags],
        }

    def _calculate_streak(self) -> int:
        """计算连续记录天数（从最近有笔记的日期向前数）。"""
        rows = self.conn.execute(
            "SELECT DISTINCT trade_date FROM review_notes ORDER BY trade_date DESC"
        ).fetchall()
        if not rows:
            return 0

        dates = [r["trade_date"] for r in rows]
        import datetime
        today = datetime.date.today()
        # 最近笔记日期
        try:
            latest = datetime.date.fromisoformat(dates[0])
        except (ValueError, TypeError):
            return 0

        # 如果最近笔记日期不是今天或昨天，连续天数为 0
        if (today - latest).days > 1:
            return 0

        streak = 0
        expected = latest
        for d_str in dates:
            try:
                d = datetime.date.fromisoformat(d_str)
            except (ValueError, TypeError):
                break
            if d == expected:
                streak += 1
                expected = d - datetime.timedelta(days=1)
            elif d < expected:
                break
        return streak

    def count_notes_by_date(self, trade_date: str) -> int:
        """获取某交易日笔记数量。"""
        row = self.conn.execute(
            "SELECT COUNT(*) FROM review_notes WHERE trade_date = ?",
            (trade_date,),
        ).fetchone()
        return row[0] if row else 0

    # ------------------------------------------------------------------
    # 8a. 笔记-标的关联
    # ------------------------------------------------------------------

    def _get_note_stocks(self, note_id: int) -> list[dict[str, str]]:
        """获取某笔记关联的标的列表。"""
        rows = self.conn.execute(
            "SELECT stock_code AS code, stock_name AS name "
            "FROM review_note_stocks WHERE note_id = ? ORDER BY id",
            (note_id,),
        ).fetchall()
        return self._rows_to_list(rows)

    def _get_note_stocks_batch(self, note_ids: list[int]) -> dict[int, list[dict[str, str]]]:
        """批量获取多个笔记关联的标的。一次 SQL 查询，按 note_id 分组。"""
        if not note_ids:
            return {}
        placeholders = ",".join(["?"] * len(note_ids))
        rows = self.conn.execute(
            f"SELECT note_id, stock_code AS code, stock_name AS name "
            f"FROM review_note_stocks WHERE note_id IN ({placeholders}) ORDER BY id",
            note_ids,
        ).fetchall()
        result: dict[int, list[dict[str, str]]] = {nid: [] for nid in note_ids}
        for r in rows:
            nid = r["note_id"]
            if nid in result:
                result[nid].append({"code": r["code"], "name": r["name"]})
        return result

    def get_notes_by_stock(self, stock_code: str,
                           page: int = 1, page_size: int = 20) -> tuple[list[dict[str, Any]], int]:
        """按标的反查笔记。"""
        count_row = self.conn.execute(
            "SELECT COUNT(DISTINCT n.id) FROM review_notes n "
            "JOIN review_note_stocks s ON n.id = s.note_id "
            "WHERE s.stock_code = ?",
            (stock_code,),
        ).fetchone()
        total = count_row[0] if count_row else 0

        offset = (page - 1) * page_size
        rows = self.conn.execute(
            "SELECT DISTINCT n.id, n.title, n.trade_date, n.created_at "
            "FROM review_notes n "
            "JOIN review_note_stocks s ON n.id = s.note_id "
            "WHERE s.stock_code = ? "
            "ORDER BY n.trade_date DESC LIMIT ? OFFSET ?",
            (stock_code, page_size, offset),
        ).fetchall()

        notes = []
        for r in rows:
            d = self._row_to_dict(r)
            d["linkedStocks"] = self._get_note_stocks(d["id"])
            notes.append(d)

        return notes, total

    # ==================================================================
    # 统计与维护
    # ==================================================================

    # ------------------------------------------------------------------
    # 策略复盘记录 (strategy_reviews)
    # ------------------------------------------------------------------

    def create_review(self, scan_id: str, cli_tool: str, content: str) -> int:
        """创建一条策略复盘记录，返回 id。"""
        cur = self.conn.execute(
            "INSERT INTO strategy_reviews (scan_id, cli_tool, content) VALUES (?, ?, ?)",
            (scan_id, cli_tool, content),
        )
        self.conn.commit()
        return cur.lastrowid

    def get_reviews_by_scan(self, scan_id: str) -> list[dict[str, Any]]:
        """获取指定扫描的所有复盘记录（仅保留有关联笔记的）。"""
        rows = self.conn.execute(
            "SELECT sr.id, sr.scan_id, sr.cli_tool, sr.content, sr.note_id, sr.created_at, "
            "rn.title AS note_title, rn.trade_date AS note_trade_date "
            "FROM strategy_reviews sr "
            "INNER JOIN review_notes rn ON sr.note_id = rn.id "
            "WHERE sr.scan_id = ? ORDER BY sr.created_at DESC",
            (scan_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_review(self, review_id: int) -> dict[str, Any] | None:
        """获取单条复盘记录。"""
        row = self.conn.execute(
            "SELECT id, scan_id, cli_tool, content, note_id, created_at "
            "FROM strategy_reviews WHERE id = ?",
            (review_id,),
        ).fetchone()
        return dict(row) if row else None

    def link_review_note(self, review_id: int, note_id: int) -> bool:
        """将复盘记录关联到笔记。"""
        cur = self.conn.execute(
            "UPDATE strategy_reviews SET note_id = ? WHERE id = ?",
            (note_id, review_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def delete_review(self, review_id: int) -> bool:
        """删除一条策略复盘记录（同时删除关联笔记）。"""
        row = self.conn.execute(
            "SELECT note_id FROM strategy_reviews WHERE id = ?",
            (review_id,),
        ).fetchone()
        if not row:
            return False
        note_id = row["note_id"]
        if note_id:
            self.conn.execute("DELETE FROM review_notes WHERE id = ?", (note_id,))
        self.conn.execute("DELETE FROM strategy_reviews WHERE id = ?", (review_id,))
        self.conn.commit()
        return True

    # ==================================================================
    # 12. 策略参数配置 (strategy_params)
    # ==================================================================

    # ---- 各策略默认参数种子数据 ----
    STRATEGY_DEFAULT_PARAMS: list[dict[str, Any]] = [
        # ---- S1 趋势跟随 ----
        {"strategy": "s1", "name": "ma_alignment_map", "label": "多头排列打分映射", "type": "range",
         "default": "[0,5,10,16,20]"},
        {"strategy": "s1", "name": "trend_health_center", "label": "趋势健康高斯中心", "type": "float",
         "default": "5.0", "min": 2.0, "max": 10.0, "step": 0.5},
        {"strategy": "s1", "name": "trend_health_sigma", "label": "趋势健康高斯sigma", "type": "float",
         "default": "4.0", "min": 2.0, "max": 8.0, "step": 0.5},
        {"strategy": "s1", "name": "trend_health_max", "label": "趋势健康满分", "type": "float",
         "default": "15.0", "min": 10.0, "max": 20.0, "step": 1.0},
        {"strategy": "s1", "name": "pullback_ma10_tight", "label": "紧贴MA10距离阈值%", "type": "float",
         "default": "1.0", "min": 0.3, "max": 3.0, "step": 0.1},
        {"strategy": "s1", "name": "pullback_ma10_near", "label": "贴近MA10距离阈值%", "type": "float",
         "default": "3.0", "min": 1.0, "max": 6.0, "step": 0.2},
        {"strategy": "s1", "name": "pullback_scores", "label": "回调支撑各档分数", "type": "range",
         "default": "[15,12,9,5,4,0]"},
        {"strategy": "s1", "name": "vol_healthy_center", "label": "量价健康高斯中心", "type": "float",
         "default": "1.4", "min": 1.0, "max": 2.0, "step": 0.1},
        {"strategy": "s1", "name": "vol_healthy_sigma", "label": "量价健康高斯sigma", "type": "float",
         "default": "0.4", "min": 0.2, "max": 0.8, "step": 0.05},
        {"strategy": "s1", "name": "env_bearish_mult", "label": "熊市环境乘数", "type": "float",
         "default": "0.8", "min": 0.5, "max": 1.0, "step": 0.05},
        {"strategy": "s1", "name": "min_score_threshold", "label": "最低入选得分", "type": "float",
         "default": "1.0", "min": 0.0, "max": 20.0, "step": 1.0},
        # ---- S2 底部反转 ----
        {"strategy": "s2", "name": "rsi_divergence_full", "label": "RSI背离满分", "type": "float",
         "default": "25.0", "min": 15.0, "max": 35.0, "step": 1.0},
        {"strategy": "s2", "name": "rsi_oversold_deep", "label": "RSI深度超卖阈值", "type": "float",
         "default": "30.0", "min": 20.0, "max": 40.0, "step": 1.0},
        {"strategy": "s2", "name": "rsi_oversold_mild", "label": "RSI轻度超卖阈值", "type": "float",
         "default": "40.0", "min": 35.0, "max": 50.0, "step": 1.0},
        {"strategy": "s2", "name": "oversold_scores", "label": "超卖各档分数", "type": "range",
         "default": "[20,16,10,5,0]"},
        {"strategy": "s2", "name": "boll_oversold_thresholds", "label": "布林超卖各档阈值", "type": "range",
         "default": "[0.1,0.2,0.3,0.4]"},
        {"strategy": "s2", "name": "volume_drying_threshold", "label": "缩量止跌阈值", "type": "float",
         "default": "0.6", "min": 0.3, "max": 0.8, "step": 0.05},
        {"strategy": "s2", "name": "volume_drying_scores", "label": "缩量各档分数", "type": "range",
         "default": "[15,10,5,0]"},
        {"strategy": "s2", "name": "decline_enough_min", "label": "跌幅充分下限%", "type": "float",
         "default": "-15.0", "min": -25.0, "max": -5.0, "step": 1.0},
        {"strategy": "s2", "name": "decline_enough_max", "label": "跌幅充分上限%", "type": "float",
         "default": "-5.0", "min": -10.0, "max": -2.0, "step": 0.5},
        {"strategy": "s2", "name": "accelerate_down_penalty", "label": "加速下跌惩罚阈值%", "type": "float",
         "default": "-5.0", "min": -8.0, "max": -2.0, "step": 0.5},
        {"strategy": "s2", "name": "env_bullish_mult", "label": "牛市环境乘数", "type": "float",
         "default": "0.7", "min": 0.5, "max": 1.0, "step": 0.05},
        {"strategy": "s2", "name": "env_bearish_mult", "label": "熊市环境乘数", "type": "float",
         "default": "1.2", "min": 1.0, "max": 1.5, "step": 0.05},
        # ---- S3 动量突破 ----
        {"strategy": "s3", "name": "breakout_scores", "label": "突破强度各档分数", "type": "range",
         "default": "[20,15,12,6,0]"},
        {"strategy": "s3", "name": "breakout_vol_thresholds", "label": "突破量比各档阈值", "type": "range",
         "default": "[2.0,1.8,1.5,1.2,1.0]"},
        {"strategy": "s3", "name": "breakout_vol_scores", "label": "突破量比各档分数", "type": "range",
         "default": "[18,14,10,6,3,0]"},
        {"strategy": "s3", "name": "rsi_momentum_center", "label": "RSI动能高斯中心", "type": "float",
         "default": "63.0", "min": 55.0, "max": 75.0, "step": 1.0},
        {"strategy": "s3", "name": "rsi_momentum_sigma", "label": "RSI动能高斯sigma", "type": "float",
         "default": "6.0", "min": 3.0, "max": 10.0, "step": 0.5},
        {"strategy": "s3", "name": "base_quality_max_amp", "label": "蓄势振幅上限", "type": "float",
         "default": "8.0", "min": 5.0, "max": 15.0, "step": 0.5},
        {"strategy": "s3", "name": "base_quality_scores", "label": "蓄势质量各档分数", "type": "range",
         "default": "[10,7,4,0]"},
        {"strategy": "s3", "name": "chase_high_penalty", "label": "追高惩罚涨幅阈值%", "type": "float",
         "default": "12.0", "min": 8.0, "max": 18.0, "step": 0.5},
        {"strategy": "s3", "name": "upper_shadow_penalty", "label": "上影线惩罚阈值%", "type": "float",
         "default": "30.0", "min": 20.0, "max": 50.0, "step": 2.0},
        {"strategy": "s3", "name": "env_nontrending_mult", "label": "震荡市环境乘数", "type": "float",
         "default": "0.7", "min": 0.5, "max": 1.0, "step": 0.05},
    ]

    def init_strategy_params(self):
        """初始化策略默认参数（幂等：已存在的参数不会覆盖）。"""
        for p in self.STRATEGY_DEFAULT_PARAMS:
            try:
                self.conn.execute(
                    "INSERT OR IGNORE INTO strategy_params "
                    "(strategy, param_name, param_label, param_type, current_value, default_value, "
                    "min_value, max_value, step) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        p["strategy"], p["name"], p["label"], p.get("type", "float"),
                        p["default"], p["default"],
                        p.get("min"), p.get("max"), p.get("step"),
                    ),
                )
            except Exception:
                pass
        self.conn.commit()

    def get_strategy_params(self, strategy: str) -> list[dict[str, Any]]:
        """获取指定策略的所有参数配置。"""
        rows = self.conn.execute(
            "SELECT id, strategy, param_name, param_label, param_type, "
            "current_value, default_value, min_value, max_value, step, "
            "last_tuned, tune_history_json "
            "FROM strategy_params WHERE strategy = ? ORDER BY id",
            (strategy,),
        ).fetchall()
        return self._rows_to_list(rows)

    def update_strategy_param(self, strategy: str, param_name: str, new_value: str,
                              reason: str = "manual") -> bool:
        """更新单个策略参数，记录调整历史。"""
        row = self.conn.execute(
            "SELECT current_value, tune_history_json FROM strategy_params "
            "WHERE strategy = ? AND param_name = ?",
            (strategy, param_name),
        ).fetchone()
        if not row:
            return False
        old_value = row["current_value"]
        history = json.loads(row["tune_history_json"]) if row["tune_history_json"] else []
        history.append({
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "old_value": old_value,
            "new_value": new_value,
            "reason": reason,
        })
        self.conn.execute(
            "UPDATE strategy_params SET current_value = ?, last_tuned = datetime('now', '+8 hours'), "
            "tune_history_json = ? WHERE strategy = ? AND param_name = ?",
            (new_value, json.dumps(history, ensure_ascii=False), strategy, param_name),
        )
        self.conn.commit()
        return True

    def reset_strategy_params(self, strategy: str) -> int:
        """重置某策略所有参数为默认值，返回重置数量。"""
        cur = self.conn.execute(
            "UPDATE strategy_params SET current_value = default_value, last_tuned = NULL, "
            "tune_history_json = '[]' WHERE strategy = ?",
            (strategy,),
        )
        self.conn.commit()
        return cur.rowcount

    # ==================================================================
    # 13. 精筛记录 (precision_picks)
    # ==================================================================

    def save_precision_picks(self, trade_date: str, picks: list[dict[str, Any]]) -> int:
        """批量保存精筛结果。返回写入行数。"""
        if not picks:
            return 0
        rows = [
            (
                trade_date,
                p["code"],
                p["name"],
                float(p.get("pick_price", 0)),
                float(p.get("precision_score", 0)),
                int(p.get("rank", i + 1)),
                json.dumps(p.get("reasons", []), ensure_ascii=False),
                json.dumps(p.get("feature_scores", {}), ensure_ascii=False),
                json.dumps(p.get("signal_weights", {}), ensure_ascii=False),
            )
            for i, p in enumerate(picks)
        ]
        with self.conn:
            self.conn.executemany(
                "INSERT OR REPLACE INTO precision_picks(trade_date, code, name, pick_price, "
                "precision_score, rank, reasons_json, feature_scores_json, signal_weights_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        return len(rows)

    def get_precision_picks(self, trade_date: str) -> list[dict[str, Any]]:
        """获取指定交易日的精筛结果。"""
        rows = self.conn.execute(
            "SELECT * FROM precision_picks WHERE trade_date = ? ORDER BY rank",
            (trade_date,),
        ).fetchall()
        results = self._rows_to_list(rows)
        for r in results:
            r["reasons"] = json.loads(r.pop("reasons_json", "[]"))
            r["feature_scores"] = json.loads(r.pop("feature_scores_json", "{}"))
            r["signal_weights"] = json.loads(r.pop("signal_weights_json", "{}"))
        return results

    def get_today_precision_picks(self) -> list[dict[str, Any]]:
        """获取最新交易日的精筛结果。"""
        row = self.conn.execute(
            "SELECT MAX(trade_date) FROM precision_picks"
        ).fetchone()
        if not row or not row[0]:
            return []
        return self.get_precision_picks(row[0])

    def get_precision_history(self, page: int = 1, page_size: int = 20,
                              outcome: str | None = None) -> tuple[list[dict[str, Any]], int]:
        """分页获取历史精筛记录。"""
        where = ""
        params: list[Any] = []
        if outcome:
            where = "WHERE outcome = ?"
            params.append(outcome)

        count_row = self.conn.execute(
            f"SELECT COUNT(*) FROM precision_picks {where}", params
        ).fetchone()
        total = count_row[0] if count_row else 0

        offset = (page - 1) * page_size
        rows = self.conn.execute(
            f"SELECT * FROM precision_picks {where} ORDER BY trade_date DESC, rank ASC "
            f"LIMIT ? OFFSET ?",
            params + [page_size, offset],
        ).fetchall()
        results = self._rows_to_list(rows)
        for r in results:
            r["reasons"] = json.loads(r.pop("reasons_json", "[]"))
            r["feature_scores"] = json.loads(r.pop("feature_scores_json", "{}"))
            r["signal_weights"] = json.loads(r.pop("signal_weights_json", "{}"))
        return results, total

    def update_precision_outcomes(self) -> int:
        """更新所有 pending 状态精筛股的 latest_price 和 return_pct。返回更新数。"""
        pending = self.conn.execute(
            "SELECT id, code, pick_price, trade_date FROM precision_picks WHERE outcome = 'pending'"
        ).fetchall()
        updated = 0
        for p in pending:
            row = self.conn.execute(
                "SELECT close FROM daily_kline WHERE code = ? ORDER BY trade_date DESC LIMIT 1",
                (p["code"],),
            ).fetchone()
            if row:
                latest = row["close"]
                ret = round((latest - p["pick_price"]) / p["pick_price"] * 100, 2)
                self.conn.execute(
                    "UPDATE precision_picks SET latest_price = ?, return_pct = ? WHERE id = ?",
                    (latest, ret, p["id"]),
                )
                updated += 1
        self.conn.commit()
        return updated

    def set_precision_outcome(self, pick_id: int, outcome: str):
        """手动设置精筛股 outcome。"""
        self.conn.execute(
            "UPDATE precision_picks SET outcome = ?, outcome_verified_at = datetime('now', '+8 hours') "
            "WHERE id = ?",
            (outcome, pick_id),
        )
        self.conn.commit()

    def auto_judge_outcomes(self) -> int:
        """自动判定到期精筛股的 outcome。返回判定数。"""
        judged = 0
        pending = self.conn.execute(
            "SELECT id, code, pick_price, trade_date, return_pct, outcome_days "
            "FROM precision_picks WHERE outcome = 'pending'"
        ).fetchall()
        for p in pending:
            # 检查是否已过判定窗口 (需要至少5个交易日)
            # 从 daily_kline 查入选后最高价
            max_row = self.conn.execute(
                "SELECT MAX(high) as max_high FROM daily_kline "
                "WHERE code = ? AND trade_date > ?",
                (p["code"], p["trade_date"]),
            ).fetchone()
            if not max_row or not max_row["max_high"]:
                continue

            max_high = max_row["max_high"]
            ret = p["return_pct"] or 0
            max_ret = (max_high - p["pick_price"]) / p["pick_price"] * 100

            # Count trading days since pick
            days_row = self.conn.execute(
                "SELECT COUNT(*) as cnt FROM daily_kline "
                "WHERE code = ? AND trade_date > ?",
                (p["code"], p["trade_date"]),
            ).fetchone()
            days_passed = days_row["cnt"] if days_row else 0

            if days_passed < 5:
                continue  # 不足5个交易日，保持 pending

            if ret > 3 and max_ret > 5:
                outcome = "win"
            elif ret < -3 or (ret <= -3):
                outcome = "loss"
            else:
                outcome = "breakeven"

            self.conn.execute(
                "UPDATE precision_picks SET outcome = ?, outcome_days = ?, "
                "outcome_verified_at = datetime('now', '+8 hours') "
                "WHERE id = ?",
                (outcome, days_passed, p["id"]),
            )
            judged += 1
        self.conn.commit()
        return judged

    def get_precision_performance(self) -> dict[str, Any]:
        """计算精筛引擎整体绩效统计。"""
        rows = self.conn.execute(
            "SELECT outcome, return_pct, trade_date FROM precision_picks "
            "WHERE outcome != 'pending'"
        ).fetchall()
        if not rows:
            return {
                "total_picks": 0, "judged": 0, "win_rate": 0,
                "avg_return": 0, "monthly_stats": [],
            }
        judged = [r for r in rows]
        wins = [r for r in judged if r["outcome"] == "win"]
        returns = [r["return_pct"] for r in judged if r["return_pct"] is not None]
        wr = round(len(wins) / len(judged) * 100, 1) if judged else 0
        avg_ret = round(sum(returns) / len(returns), 2) if returns else 0

        # 按月份统计
        month_map: dict[str, dict] = {}
        for r in judged:
            month = r["trade_date"][:6]
            if month not in month_map:
                month_map[month] = {"month": month, "total": 0, "wins": 0, "returns": []}
            month_map[month]["total"] += 1
            if r["outcome"] == "win":
                month_map[month]["wins"] += 1
            if r["return_pct"] is not None:
                month_map[month]["returns"].append(r["return_pct"])

        monthly = []
        for m in sorted(month_map.keys()):
            d = month_map[m]
            monthly.append({
                "month": f"{m[:4]}-{m[4:6]}",
                "total": d["total"],
                "win_rate": round(d["wins"] / d["total"] * 100, 1) if d["total"] else 0,
                "avg_return": round(sum(d["returns"]) / len(d["returns"]), 2) if d["returns"] else 0,
            })

        return {
            "total_picks": self.conn.execute("SELECT COUNT(*) FROM precision_picks").fetchone()[0],
            "judged": len(judged),
            "win_rate": wr,
            "avg_return": avg_ret,
            "monthly_stats": monthly,
        }

    # ==================================================================
    # 14. 信号权重 (signal_weights)
    # ==================================================================

    def init_signal_weights(self):
        """初始化精筛信号权重（幂等）。"""
        default_signals = [
            # 策略得分维度
            ("s1_score", "strategy_score", 1.0),
            ("s2_score", "strategy_score", 1.0),
            ("s3_score", "strategy_score", 1.0),
            # 策略共识维度
            ("consensus_2strategy", "consensus", 1.0),
            ("consensus_3strategy", "consensus", 1.0),
            ("consensus_avg_score", "consensus", 1.0),
            # 因子信号维度 — S1
            ("s1_ma_alignment", "factor_detail", 1.0),
            ("s1_trend_health", "factor_detail", 1.0),
            ("s1_pullback_support", "factor_detail", 1.0),
            ("s1_macd_strength", "factor_detail", 1.0),
            ("s1_vol_healthy", "factor_detail", 1.0),
            ("s1_boll_direction", "factor_detail", 1.0),
            ("s1_relative_strength", "factor_detail", 1.0),
            # 因子信号维度 — S2
            ("s2_rsi_divergence", "factor_detail", 1.0),
            ("s2_oversold_level", "factor_detail", 1.0),
            ("s2_volume_drying", "factor_detail", 1.0),
            ("s2_price_stabilizing", "factor_detail", 1.0),
            ("s2_macd_turning", "factor_detail", 1.0),
            ("s2_decline_enough", "factor_detail", 1.0),
            ("s2_support_test", "factor_detail", 1.0),
            # 因子信号维度 — S3
            ("s3_breakout_strength", "factor_detail", 1.0),
            ("s3_volume_surge", "factor_detail", 1.0),
            ("s3_golden_cross", "factor_detail", 1.0),
            ("s3_rsi_momentum", "factor_detail", 1.0),
            ("s3_macd_cross", "factor_detail", 1.0),
            ("s3_base_quality", "factor_detail", 1.0),
            ("s3_boll_expansion", "factor_detail", 1.0),
            # 多日持续性维度
            ("persistence_2day", "persistence", 1.0),
            ("persistence_3day", "persistence", 1.0),
            ("persistence_streak_score", "persistence", 1.0),
            # 市场适配维度
            ("market_s1_fit", "market_env", 1.0),
            ("market_s2_fit", "market_env", 1.0),
            ("market_s3_fit", "market_env", 1.0),
            ("market_phase_score", "market_env", 1.0),
        ]
        for name, cat, weight in default_signals:
            self.conn.execute(
                "INSERT OR IGNORE INTO signal_weights(signal_name, category, weight) "
                "VALUES (?, ?, ?)",
                (name, cat, weight),
            )
        self.conn.commit()

    def get_signal_weights(self) -> list[dict[str, Any]]:
        """获取所有信号权重（按类别分组）。"""
        rows = self.conn.execute(
            "SELECT * FROM signal_weights ORDER BY category, signal_name"
        ).fetchall()
        return self._rows_to_list(rows)

    def get_signal_weights_map(self) -> dict[str, dict[str, Any]]:
        """获取信号权重的 dict 映射，key=signal_name。"""
        rows = self.get_signal_weights()
        return {r["signal_name"]: r for r in rows}

    def update_signal_weight(self, signal_name: str, weight: float, sample_count: int,
                             win_rate: float, avg_return: float, ic: float) -> bool:
        """更新单个信号权重和统计信息。"""
        self.conn.execute(
            "UPDATE signal_weights SET weight = ?, sample_count = ?, "
            "win_rate = ?, avg_return = ?, information_coef = ?, "
            "last_updated = datetime('now', '+8 hours') "
            "WHERE signal_name = ?",
            (weight, sample_count, win_rate, avg_return, ic, signal_name),
        )
        self.conn.commit()
        return True

    def update_signal_samples(self, signal_name: str, sample_count: int,
                              positive_count: int, win_rate: float,
                              avg_return: float) -> bool:
        """仅更新信号统计信息（样本数、胜率），不改变权重。"""
        self.conn.execute(
            "UPDATE signal_weights SET sample_count = ?, positive_count = ?, "
            "win_rate = ?, avg_return = ?, "
            "last_updated = datetime('now', '+8 hours') "
            "WHERE signal_name = ?",
            (sample_count, positive_count, win_rate, avg_return, signal_name),
        )
        self.conn.commit()
        return True

    # ==================================================================
    # 15. 精筛每日日志 (precision_daily_log)
    # ==================================================================

    def save_precision_log(self, trade_date: str, total_candidates: int,
                           picks_count: int, weights_snapshot: dict,
                           params_snapshot: dict) -> None:
        """记录每日精筛执行日志。"""
        self.conn.execute(
            "INSERT OR REPLACE INTO precision_daily_log "
            "(trade_date, total_candidates, picks_count, weights_snapshot_json, params_snapshot_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                trade_date, total_candidates, picks_count,
                json.dumps(weights_snapshot, ensure_ascii=False),
                json.dumps(params_snapshot, ensure_ascii=False),
            ),
        )
        self.conn.commit()

    def get_precision_log(self, trade_date: str) -> dict[str, Any] | None:
        """获取某日精筛日志。"""
        row = self.conn.execute(
            "SELECT * FROM precision_daily_log WHERE trade_date = ?",
            (trade_date,),
        ).fetchone()
        if not row:
            return None
        d = self._row_to_dict(row)
        d["weights_snapshot"] = json.loads(d.pop("weights_snapshot_json", "{}"))
        d["params_snapshot"] = json.loads(d.pop("params_snapshot_json", "{}"))
        return d

    def get_latest_precision_date(self) -> str | None:
        """获取最近一次精筛日期。"""
        row = self.conn.execute(
            "SELECT MAX(trade_date) FROM precision_daily_log"
        ).fetchone()
        return row[0] if row else None

    # ==================================================================
    # 16. 因子有效性 (factor_effectiveness)
    # ==================================================================

    def update_factor_effectiveness(self, strategy: str, factor_name: str,
                                    sample_count: int, positive_count: int,
                                    win_rate: float, avg_return: float,
                                    ic: float) -> None:
        """更新因子有效性统计。"""
        self.conn.execute(
            "INSERT OR REPLACE INTO factor_effectiveness "
            "(strategy, factor_name, sample_count, positive_count, win_rate, avg_return, ic, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now', '+8 hours'))",
            (strategy, factor_name, sample_count, positive_count, win_rate, avg_return, ic),
        )
        self.conn.commit()

    def get_factor_effectiveness(self, strategy: str | None = None) -> list[dict[str, Any]]:
        """获取因子有效性报告。"""
        if strategy:
            rows = self.conn.execute(
                "SELECT * FROM factor_effectiveness WHERE strategy = ? ORDER BY ic DESC",
                (strategy,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM factor_effectiveness ORDER BY strategy, ic DESC"
            ).fetchall()
        return self._rows_to_list(rows)

    def get_strategy_dates(self, strategy: str, limit: int = 10) -> list[str]:
        """获取指定策略最近有结果的交易日列表。"""
        rows = self.conn.execute(
            "SELECT DISTINCT trade_date FROM strategy_results WHERE strategy = ? "
            "ORDER BY trade_date DESC LIMIT ?",
            (strategy, limit),
        ).fetchall()
        return [r["trade_date"] for r in rows]

    def get_strategy_results_by_date(self, trade_date: str, strategy: str) -> list[dict[str, Any]]:
        """获取指定交易日和策略的所有结果（含因子明细）。"""
        rows = self.conn.execute(
            "SELECT sr.code, sr.name, sr.score, sr.rank, sr.factors_json, sr.signals_json, sr.metrics_json "
            "FROM strategy_results sr "
            "INNER JOIN strategy_scan_log sl ON sr.scan_id = sl.scan_id "
            "WHERE sr.trade_date = ? AND sr.strategy = ? AND sl.status = 'completed' "
            "ORDER BY sr.rank",
            (trade_date, strategy),
        ).fetchall()
        results = []
        for r in rows:
            d = self._row_to_dict(r)
            d["factors_detail"] = json.loads(d.pop("factors_json", "{}"))
            d["signals"] = json.loads(d.pop("signals_json", "[]"))
            d["metrics"] = json.loads(d.pop("metrics_json", "{}"))
            results.append(d)
        return results

    def get_all_strategy_results_for_date(self, trade_date: str) -> dict[str, list[dict[str, Any]]]:
        """获取指定交易日所有已完成策略的结果，按策略key分组。"""
        result: dict[str, list[dict[str, Any]]] = {}
        for s in ["s1", "s2", "s3"]:
            data = self.get_strategy_results_by_date(trade_date, s)
            if data:
                result[s] = data
        return result

    def stats(self) -> dict[str, Any]:
        """数据库整体统计。"""
        try:
            universe_count = self.conn.execute("SELECT COUNT(*) FROM stock_universe").fetchone()[0]
            watchlist_count = self.conn.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0]
            kline_count = self.conn.execute("SELECT COUNT(*) FROM daily_kline").fetchone()[0]
            kline_stocks = self.conn.execute("SELECT COUNT(DISTINCT code) FROM daily_kline").fetchone()[0]
            quotes_count = self.conn.execute("SELECT COUNT(*) FROM stock_quotes_cache").fetchone()[0]
            db_size = os.path.getsize(self.db_path) if os.path.exists(self.db_path) else 0
            return {
                "db_path": self.db_path,
                "db_size_mb": round(db_size / 1024 / 1024, 2),
                "universe_stocks": universe_count,
                "watchlist": watchlist_count,
                "kline_rows": kline_count,
                "kline_stocks": kline_stocks,
                "cached_quotes": quotes_count,
            }
        except Exception:
            return {"db_path": self.db_path, "error": "无法读取统计"}


# ==================================================================
# 全局单例
# ==================================================================

_storage_instance: SqliteStorage | None = None
_storage_lock = threading.Lock()


def get_storage() -> SqliteStorage:
    """获取全局 SqliteStorage 单例。"""
    global _storage_instance
    if _storage_instance is None:
        with _storage_lock:
            if _storage_instance is None:
                _storage_instance = SqliteStorage()
                _storage_instance.init_default_watchlist()
                _storage_instance.init_strategy_params()
                _storage_instance.init_signal_weights()
    return _storage_instance


def _cleanup_connections() -> None:
    """进程退出时关闭所有 SQLite 连接。"""
    if _storage_instance is not None:
        _storage_instance.close_all()


atexit.register(_cleanup_connections)
