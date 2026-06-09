from fastapi import APIRouter

from app.core.response import ok
from app.services.market_calendar import get_market_status
from app.services.market_service import get_market_overview

router = APIRouter(prefix="/market", tags=["market"])


@router.get("/overview")
def overview():
    return ok(get_market_overview())


@router.get("/status")
def status():
    """交易日历 / 盘前盘中状态。"""
    return ok(get_market_status())
