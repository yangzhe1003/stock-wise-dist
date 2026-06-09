import re

CODE_RE = re.compile(r"^\d{6}$")
MARKETS = {"all", "sh", "sz", "cyb", "kcb"}
SORT_FIELDS = {"price", "changePct", "volume", "marketCap", "code", "name"}
KLINE_PERIODS = {"1m", "5m", "15m", "30m", "60m", "120m", "day", "week", "mon"}


def normalize_code(code: str) -> str:
    code = str(code).strip()
    if not CODE_RE.match(code):
        raise ValueError("股票代码必须是 6 位数字")
    return code


def normalize_market(market: str) -> str:
    value = (market or "all").strip().lower()
    if value not in MARKETS:
        raise ValueError("市场筛选参数无效")
    return value


def normalize_sort(sort: str) -> str:
    value = (sort or "price").strip()
    if value not in SORT_FIELDS:
        raise ValueError("排序字段无效")
    return value


def normalize_kline_period(period: str) -> str:
    value = (period or "day").strip().lower()
    if value not in KLINE_PERIODS:
        raise ValueError("K 线周期参数无效")
    return value
