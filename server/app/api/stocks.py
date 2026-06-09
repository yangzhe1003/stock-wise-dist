from fastapi import APIRouter, Query

from app.core.response import ok
from app.core.validation import (
    normalize_code,
    normalize_kline_period,
    normalize_market,
    normalize_sort,
)
from app.services.stock_service import (
    get_kline,
    get_minute_points,
    get_stock_detail,
    get_stock_list,
)
from app.services.stock_universe import get_universe, get_universe_status

router = APIRouter(prefix="/stocks", tags=["stocks"])


@router.get("")
def stocks(
    q: str = "",
    market: str = Query(default="all"),
    sort: str = Query(default="price"),
    order: str = Query(default="desc"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    result = get_stock_list(
        q=q, market=normalize_market(market),
        sort=normalize_sort(sort), order=order,
        page=page, page_size=page_size,
    )
    return ok(result)


@router.get("/universe/status")
def universe_status():
    return ok(get_universe_status())


@router.post("/universe/refresh")
def universe_refresh():
    items = get_universe(force_refresh=True)
    return ok({"count": len(items), "status": get_universe_status()})


@router.get("/{code}")
def detail(code: str):
    return ok(get_stock_detail(normalize_code(code)))


@router.get("/{code}/minute")
def minute(code: str):
    return ok(get_minute_points(normalize_code(code)))


@router.get("/{code}/kline")
def kline(code: str, period: str = Query(default="day")):
    return ok(get_kline(normalize_code(code), normalize_kline_period(period)))
