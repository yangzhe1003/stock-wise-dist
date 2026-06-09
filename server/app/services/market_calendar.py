"""A 股交易日历与时段判断。

判断依据：
- 周末为非交易日
- 通过 ``HOLIDAYS`` 维护 A 股调休/休市日期（YYYY-MM-DD），需每年更新一次
- 不在 ``WORKDAY_ADJUSTMENTS`` 中的周六/周日调休暂不覆盖
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone, timedelta
from typing import Any

BEIJING_TZ = timezone(timedelta(hours=8))

# A 股 2025/2026 法定休市日期，按需补充。日期格式：YYYY-MM-DD。
HOLIDAYS: set[str] = {
    # 2025 年
    "2025-01-01",  # 元旦
    "2025-01-28", "2025-01-29", "2025-01-30", "2025-01-31",  # 春节
    "2025-02-03", "2025-02-04",
    "2025-04-04", "2025-04-05", "2025-04-06",  # 清明
    "2025-05-01", "2025-05-02", "2025-05-05",  # 劳动节
    "2025-05-31", "2025-06-02",  # 端午
    "2025-10-01", "2025-10-02", "2025-10-03",  # 国庆
    "2025-10-06", "2025-10-07", "2025-10-08",
    # 2026 年（按官方公告补充）
    "2026-01-01", "2026-01-02",  # 元旦
    "2026-02-16", "2026-02-17", "2026-02-18",  # 春节
    "2026-02-19", "2026-02-20",
    "2026-04-04", "2026-04-05", "2026-04-06",  # 清明
    "2026-05-01", "2026-05-04", "2026-05-05",  # 劳动节
    "2026-06-19", "2026-06-22",  # 端午
    "2026-09-25", "2026-09-28",  # 中秋
    "2026-10-01", "2026-10-02", "2026-10-05",  # 国庆
    "2026-10-06", "2026-10-07", "2026-10-08",
}

# 因调休而补班的周末（YYYY-MM-DD）
WORKDAY_ADJUSTMENTS: set[str] = set()

# 时段划分（北京时间）
SESSION_PRE_OPEN_START = time(9, 15)
SESSION_MORNING_START = time(9, 30)
SESSION_MORNING_END = time(11, 30)
SESSION_AFTERNOON_START = time(13, 0)
SESSION_AFTERNOON_END = time(15, 0)


def beijing_now() -> datetime:
    return datetime.now(BEIJING_TZ)


def is_trading_day(d: date) -> bool:
    key = d.strftime("%Y-%m-%d")
    if key in WORKDAY_ADJUSTMENTS:
        return True
    if d.weekday() >= 5:
        return False
    return key not in HOLIDAYS


def last_close_time() -> float:
    """返回最近一次 15:00 收盘的 Unix 时间戳（盘后/周末/节假日都安全）。

    规则：
    - 今天是交易日且 now >= 15:00 → 今天 15:00
    - 否则回溯到最近一个交易日 → 该日 15:00（最多回溯 7 天，覆盖国庆+春节）
    - 兜底：直接用昨天 15:00

    用途：缓存策略判断「某条记录是否来自最近一次收盘时刻」。
    """
    now = beijing_now()
    today = now.date()
    afternoon_end = datetime.combine(today, SESSION_AFTERNOON_END, tzinfo=BEIJING_TZ)

    if is_trading_day(today) and now >= afternoon_end:
        return afternoon_end.timestamp()

    for offset in range(1, 8):
        d = today - timedelta(days=offset)
        if is_trading_day(d):
            target = datetime.combine(d, SESSION_AFTERNOON_END, tzinfo=BEIJING_TZ)
            return target.timestamp()

    # 兜底：昨天 15:00（理论上有 WORKDAY_ADJUSTMENTS 的极端情况才会触发）
    fallback = datetime.combine(today - timedelta(days=1), SESSION_AFTERNOON_END, tzinfo=BEIJING_TZ)
    return fallback.timestamp()


def phase_for(now: datetime, trading_day: bool) -> dict[str, Any]:
    """根据当前时间与是否交易日，返回阶段信息。"""
    if not trading_day:
        return {"phase": "closed", "label": "休市", "trading": False}

    t = now.time()
    if SESSION_PRE_OPEN_START <= t < SESSION_MORNING_START:
        return {"phase": "pre-open", "label": "沪市 · 集合竞价", "trading": False}
    if SESSION_MORNING_START <= t < SESSION_MORNING_END:
        return {"phase": "morning", "label": "沪市 · 交易中", "trading": True}
    if SESSION_MORNING_END <= t < SESSION_AFTERNOON_START:
        return {"phase": "lunch", "label": "沪市 · 午间休市", "trading": False}
    if SESSION_AFTERNOON_START < t < SESSION_AFTERNOON_END:
        return {"phase": "afternoon", "label": "沪市 · 交易中", "trading": True}
    if t == SESSION_AFTERNOON_END:
        return {"phase": "closing", "label": "沪市 · 收盘", "trading": False}
    if t < SESSION_PRE_OPEN_START:
        return {"phase": "pre-open", "label": "沪市 · 盘前", "trading": False}
    return {"phase": "after-hours", "label": "沪市 · 盘后", "trading": False}


def get_market_status() -> dict[str, Any]:
    now = beijing_now()
    trading_day = is_trading_day(now.date())
    phase = phase_for(now, trading_day)
    return {
        "now": now.isoformat(timespec="seconds"),
        "timezone": "Asia/Shanghai",
        "weekday": now.weekday(),
        "tradingDay": trading_day,
        **phase,
    }
