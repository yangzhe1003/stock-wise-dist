from __future__ import annotations

import threading
from typing import Any, Callable, TypeVar


T = TypeVar("T")


HQ_SERVERS: tuple[tuple[str, int], ...] = (
    ("110.41.147.114", 7709),
    ("8.129.13.54", 7709),
    ("124.70.176.52", 7709),
    ("47.100.236.28", 7709),
    ("121.36.54.217", 7709),
)


class _Slot:
    """一个方法专属的 mootdx 连接 + 互斥锁。

    TDX 协议是 request/response 单 socket 模型，多个线程共享同一 socket
    会让响应错位——详情页 3 个并行接口 (quote / minute / kline) 就是被
    这样搅坏的。每个方法分到独立 slot：不同方法的请求可以真正并发，
    同一方法的并发则串行化在自身锁上。
    """

    def __init__(self) -> None:
        self._client: Any | None = None
        self._lock = threading.Lock()

    def call(self, fn: Callable[[Any], T]) -> T:
        """在锁内执行 fn(client)。_connect 失败时锁会被释放、client 不会被缓存。"""
        with self._lock:
            if self._client is None:
                self._client = self._connect()
            return fn(self._client)

    @staticmethod
    def _connect() -> Any:
        from mootdx.quotes import Quotes

        last_error: Exception | None = None
        for host, port in HQ_SERVERS:
            try:
                client = Quotes.factory(market="std", host=host, port=port, timeout=5)
                probe = client.quotes(symbol="600519")
                if probe is not None and not probe.empty:
                    return client
            except Exception as exc:
                last_error = exc
        raise RuntimeError(f"mootdx 行情服务器不可用：{last_error}")


class MootdxClient:
    """按方法拆分连接的 mootdx 包装器。

    - 详情页 3 个并行接口 (quote / minutes / bars) 走各自 slot，互不抢占
    - 同方法并发通过 slot 内置锁串行化
    - 旧接口 get_client / get_detail_client 仍保留以兼容旧代码，但持有
      的是 quote 槽的连接，调用方应保证不跨方法混用
    """

    def __init__(self) -> None:
        self._quote_slot = _Slot()
        self._stocks_slot = _Slot()
        self._minute_slot = _Slot()
        self._bars_slot = _Slot()
        self._index_bars_slot = _Slot()
        self._finance_slot = _Slot()

    # ---- 推荐接口（按方法）----

    def quote(self, symbol):
        """单只或批量 quotes 请求。"""
        return self._quote_slot.call(lambda c: c.quotes(symbol=symbol))

    def minute(self, symbol):
        return self._minute_slot.call(lambda c: c.minute(symbol=symbol))

    def minutes(self, symbol: str, date: str):
        """指定日期的历史分钟线（分时）。"""
        return self._minute_slot.call(lambda c: c.minutes(symbol=symbol, date=date))

    def stocks(self, market: int):
        """股票列表（按市场：0=深 1=沪）。"""
        return self._stocks_slot.call(lambda c: c.stocks(market=market))

    def bars(self, symbol: str, frequency: int, offset: int = 100):
        """K 线 bars 请求。"""
        return self._bars_slot.call(
            lambda c: c.bars(symbol=symbol, frequency=frequency, offset=offset)
        )

    def index_bars(self, symbol: str, frequency: int, offset: int = 100):
        """指数 K 线。走独立 index_bars slot。"""
        return self._index_bars_slot.call(
            lambda c: c.index_bars(symbol=symbol, frequency=frequency, offset=offset)
        )

    def finance_info(self, market: int, code: str):
        """财务信息（直连底层 TDX 客户端）。"""
        return self._finance_slot.call(
            lambda c: c.client.get_finance_info(market, code)
        )

    # ---- 旧接口（向后兼容，慎用）----

    def get_client(self):
        """批量任务旧接口——返回 quote 槽的连接。

        ⚠️ 新代码应直接用 quote() / stocks() / minutes() / bars() / finance_info()。
        """
        return self._quote_slot.call(lambda c: c)

    def get_detail_client(self):
        """详情页旧接口——返回 quote 槽的连接。

        ⚠️ 新代码应按方法调用 quote() / minutes() / bars() / stocks()，
        让不同方法走独立 slot，避免并发串扰。
        """
        return self._quote_slot.call(lambda c: c)


client = MootdxClient()
