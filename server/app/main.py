from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import market, notes, precision, review, stocks, strategy, strategy_params, watchlist
from app.core.response import fail, ok

app = FastAPI(title="StockBench API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:3000",
        "http://localhost:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(ValueError)
async def value_error_handler(_: Request, exc: ValueError):
    return JSONResponse(status_code=400, content=fail(str(exc), code=4001))


@app.exception_handler(Exception)
async def generic_error_handler(_: Request, exc: Exception):
    return JSONResponse(status_code=500, content=fail(f"服务异常：{exc}", code=5000))


@app.get("/api/health")
def health():
    from datetime import datetime, timezone, timedelta

    tz = timezone(timedelta(hours=8))
    return ok({"status": "ok", "time": datetime.now(tz).isoformat(timespec="seconds")})


app.include_router(market.router, prefix="/api")
app.include_router(stocks.router, prefix="/api")
app.include_router(strategy.router, prefix="/api")
app.include_router(watchlist.router, prefix="/api")
app.include_router(notes.router, prefix="/api")
app.include_router(review.router, prefix="/api")
app.include_router(precision.router, prefix="/api")
app.include_router(strategy_params.router, prefix="/api")
