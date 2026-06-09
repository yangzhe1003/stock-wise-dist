"""技术指标计算模块。

提供 A 股技术分析所需的全部指标计算：
- 均线系统（MA/EMA）
- MACD（含金叉/死叉信号）
- RSI（含背离检测）
- 布林带（含斜率）
- ATR 平均真实波幅
- 量价关系（量比、均量）
- 涨跌幅（多周期）
- N 日最高/最低
- 均线交叉与多头排列评分
- 相对强度（vs 基准）

所有函数接收 pandas Series/DataFrame，返回 Series/DataFrame，
完全不依赖外部服务，可以在扫描过程中独立运算。
"""

from __future__ import annotations

import numpy as np
import pandas as pd


# ==========================================================================
# 均线系统
# ==========================================================================

def calc_ma(series: pd.Series, window: int) -> pd.Series:
    """简单移动平均线。"""
    return series.rolling(window=window, min_periods=1).mean()


def calc_ema(series: pd.Series, window: int) -> pd.Series:
    """指数移动平均线。"""
    return series.ewm(span=window, adjust=False).mean()


# ==========================================================================
# MACD
# ==========================================================================

def calc_macd(
    close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> pd.DataFrame:
    """MACD 指标。

    Returns:
        DataFrame with columns: dif, dea, macd_hist
    """
    ema_fast = calc_ema(close, fast)
    ema_slow = calc_ema(close, slow)
    dif = ema_fast - ema_slow
    dea = calc_ema(dif, signal)
    macd_hist = 2 * (dif - dea)
    return pd.DataFrame({"dif": dif, "dea": dea, "macd_hist": macd_hist})


def calc_macd_cross(dif: pd.Series, dea: pd.Series) -> pd.Series:
    """MACD 金叉/死叉: 1=金叉(DIF上穿DEA), -1=死叉, 0=无。"""
    cross = pd.Series(0, index=dif.index)
    cond_up = (dif > dea) & (dif.shift(1) <= dea.shift(1))
    cond_down = (dif < dea) & (dif.shift(1) >= dea.shift(1))
    cross[cond_up] = 1
    cross[cond_down] = -1
    return cross


# ==========================================================================
# RSI
# ==========================================================================

def calc_rsi(close: pd.Series, window: int = 14) -> pd.Series:
    """RSI 相对强弱指标。"""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(window=window, min_periods=1).mean()
    avg_loss = loss.rolling(window=window, min_periods=1).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def calc_rsi_divergence(
    close: pd.Series, rsi: pd.Series, window: int = 20
) -> pd.Series:
    """RSI 底背离检测（向量化版本）。

    在 window 窗口内，价格创出新低但 RSI 没有创出新低，
    表示下跌动能衰竭，可能反转向上。
    用 numpy sliding_window_view 替代原来的 Python for 循环，
    5000 只股票从 ~3000 万次循环降为一次向量化操作。

    Returns:
        pd.Series: 1=底背离, 0=无背离
    """
    result = pd.Series(0, index=close.index, dtype=int)
    n = len(close)
    if n < window:
        return result

    from numpy.lib.stride_tricks import sliding_window_view

    close_arr = close.values.astype(np.float64)
    rsi_arr = rsi.values.astype(np.float64)
    win_size = window + 1  # [i-window, i] inclusive

    # 每个窗口 [i-win_size+1, i] 的 OHLC，按 axis=1 取 argmin
    price_win = sliding_window_view(close_arr, win_size)   # (n-win_size+1, win_size)
    rsi_win = sliding_window_view(rsi_arr, win_size)

    price_argmin = np.argmin(price_win, axis=1)  # 每窗价格最低点位置
    rsi_argmin = np.argmin(rsi_win, axis=1)       # 每窗 RSI 最低点位置

    # 价格最低 ≠ RSI 最低 → 背离候选
    diff_min = price_argmin != rsi_argmin

    # RSI 在价格最低点的值 vs 窗口内 RSI 最低值
    idx = np.arange(len(price_win))
    rsi_at_price_low = rsi_win[idx, price_argmin]
    rsi_low = rsi_win[idx, rsi_argmin]

    # 严格条件：RSI 在价格低点高出 5%
    diverged = diff_min & (rsi_at_price_low > rsi_low * 1.05)

    # 信号放在窗口末尾的位置（对应原始循环中的 result.iloc[i]）
    result.iloc[window:] = diverged.astype(int)

    return result


# ==========================================================================
# 布林带
# ==========================================================================

def calc_bollinger(
    close: pd.Series, window: int = 20, num_std: float = 2.0
) -> pd.DataFrame:
    """布林带。

    Returns:
        DataFrame with columns: boll_mid, boll_upper, boll_lower, boll_width, boll_pct_b
    """
    middle = calc_ma(close, window)
    std = close.rolling(window=window, min_periods=1).std()
    upper = middle + num_std * std
    lower = middle - num_std * std
    width = upper - lower
    pct_b = (close - lower) / (width.replace(0, np.nan))
    return pd.DataFrame({
        "boll_mid": middle,
        "boll_upper": upper,
        "boll_lower": lower,
        "boll_width": width,
        "boll_pct_b": pct_b,
    })


def calc_bollinger_slope(boll_mid: pd.Series, window: int = 5) -> pd.Series:
    """布林带中轨斜率（正值 = 上行趋势，负值 = 下行趋势）。"""
    return boll_mid.diff(window)


# ==========================================================================
# ATR
# ==========================================================================

def calc_atr(
    high: pd.Series, low: pd.Series, close: pd.Series, window: int = 14
) -> pd.Series:
    """ATR 平均真实波幅。"""
    tr = pd.concat(
        [
            (high - low).abs(),
            (high - close.shift()).abs(),
            (low - close.shift()).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.rolling(window=window, min_periods=1).mean()


# ==========================================================================
# 量价关系
# ==========================================================================

def calc_volume_ma(volume: pd.Series, window: int = 20) -> pd.Series:
    """成交量均线。"""
    return volume.rolling(window=window, min_periods=1).mean()


def calc_volume_ratio(volume: pd.Series, window: int = 5) -> pd.Series:
    """量比 = 当前成交量 / 过去N日均量。"""
    ma_vol = calc_volume_ma(volume, window)
    return volume / ma_vol.replace(0, np.nan)


# ==========================================================================
# 涨跌幅
# ==========================================================================

def calc_pct_change(close: pd.Series, periods: int = 1) -> pd.Series:
    """涨跌幅（%）。"""
    return close.pct_change(periods=periods) * 100


# ==========================================================================
# N 日最高/最低
# ==========================================================================

def calc_high_n(high: pd.Series, window: int) -> pd.Series:
    """N 日最高价。"""
    return high.rolling(window=window, min_periods=1).max()


def calc_low_n(low: pd.Series, window: int) -> pd.Series:
    """N 日最低价。"""
    return low.rolling(window=window, min_periods=1).min()


# ==========================================================================
# 均线交叉
# ==========================================================================

def calc_ma_cross(short_ma: pd.Series, long_ma: pd.Series) -> pd.Series:
    """均线金叉/死叉信号：1=金叉, -1=死叉, 0=无。"""
    cross = pd.Series(0, index=short_ma.index)
    cond_up = (short_ma > long_ma) & (short_ma.shift(1) <= long_ma.shift(1))
    cond_down = (short_ma < long_ma) & (short_ma.shift(1) >= long_ma.shift(1))
    cross[cond_up] = 1
    cross[cond_down] = -1
    return cross


# ==========================================================================
# 多头排列评分
# ==========================================================================

def calc_ma_arrangement(df: pd.DataFrame, ma_windows: list[int] = (5, 10, 20, 60)) -> pd.Series:
    """多头排列得分。

    对每个相邻均线对：短 > 长得 1 分。
    最大得分 = len(ma_windows) - 1
    """
    mas = {f"ma{w}": calc_ma(df["close"], w) for w in ma_windows}
    score = pd.Series(0, index=df.index)
    for i in range(len(ma_windows) - 1):
        short = ma_windows[i]
        long = ma_windows[i + 1]
        score += (mas[f"ma{short}"] > mas[f"ma{long}"]).astype(int)
    return score


# ==========================================================================
# 相对强度（vs 基准）
# ==========================================================================

def calc_relative_strength(
    close: pd.Series, benchmark_close: pd.Series, window: int = 5
) -> pd.Series:
    """相对强度：个股 N 日涨幅 - 基准 N 日涨幅（%）。

    正值表示跑赢基准，负值表示跑输。
    """
    stock_pct = calc_pct_change(close, window)
    bench_pct = calc_pct_change(benchmark_close, window)
    return stock_pct - bench_pct


# ==========================================================================
# 价格振幅
# ==========================================================================

def calc_amplitude(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """日内振幅（%）= (最高 - 最低) / 昨收 * 100。"""
    return (high - low) / close.shift(1) * 100


# ==========================================================================
# 下影线比例
# ==========================================================================

def calc_lower_shadow(open_: pd.Series, close: pd.Series, low: pd.Series) -> pd.Series:
    """下影线比例 = (min(open, close) - low) / (high - low)。"""
    body_low = pd.concat([open_, close], axis=1).min(axis=1)
    body_range = (pd.concat([open_, close], axis=1).max(axis=1) - body_low).replace(0, np.nan)
    return (body_low - low) / body_range * 100


# ==========================================================================
# 上影线比例
# ==========================================================================

def calc_upper_shadow(open_: pd.Series, high: pd.Series, close: pd.Series) -> pd.Series:
    """上影线比例 = (high - max(open, close)) / (high - low)。"""
    body_high = pd.concat([open_, close], axis=1).max(axis=1)
    body_range = (high - pd.concat([open_, close], axis=1).min(axis=1)).replace(0, np.nan)
    return (high - body_high) / body_range * 100


# ==========================================================================
# 批量指标计算（用于策略筛选）
# ==========================================================================

def enrich_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """为日线 DataFrame 批量添加所有技术指标。

    输入 DataFrame 需包含: open, high, low, close, vol, amount
    按 trade_date 升序排列（调用方保证，本函数不再重复排序）。
    """
    # 调用方保证输入已按 trade_date 升序，直接原地添加指标列
    # （不 copy：策略扫描有 ~5000 只股票，每只都 copy 一次是一笔可观开销）

    c = df["close"]
    h = df["high"]
    l = df["low"]
    o = df["open"]
    v = df["vol"]

    # ---- 均线 ----
    df["ma5"] = calc_ma(c, 5)
    df["ma10"] = calc_ma(c, 10)
    df["ma20"] = calc_ma(c, 20)
    df["ma60"] = calc_ma(c, 60)
    df["ma120"] = calc_ma(c, 120)

    # ---- MACD ----
    macd = calc_macd(c)
    df["macd_dif"] = macd["dif"]
    df["macd_dea"] = macd["dea"]
    df["macd_hist"] = macd["macd_hist"]
    df["macd_cross"] = calc_macd_cross(df["macd_dif"], df["macd_dea"])

    # ---- RSI ----
    df["rsi6"] = calc_rsi(c, 6)
    df["rsi14"] = calc_rsi(c, 14)

    # ---- RSI 背离 ----
    df["rsi_divergence"] = calc_rsi_divergence(c, df["rsi14"], 20)

    # ---- 布林带 ----
    boll = calc_bollinger(c)
    df["boll_mid"] = boll["boll_mid"]
    df["boll_upper"] = boll["boll_upper"]
    df["boll_lower"] = boll["boll_lower"]
    df["boll_width"] = boll["boll_width"]
    df["boll_pct_b"] = boll["boll_pct_b"]
    df["boll_slope"] = calc_bollinger_slope(df["boll_mid"], 5)

    # ---- 量价 ----
    df["vol_ma5"] = calc_volume_ma(v, 5)
    df["vol_ma20"] = calc_volume_ma(v, 20)
    df["vol_ratio5"] = calc_volume_ratio(v, 5)
    df["vol_ratio20"] = calc_volume_ratio(v, 20)

    # ---- 涨跌幅 ----
    df["pct_chg"] = calc_pct_change(c, 1)
    df["pct_chg5"] = calc_pct_change(c, 5)
    df["pct_chg20"] = calc_pct_change(c, 20)

    # ---- 高低位 ----
    df["high_5"] = calc_high_n(h, 5)
    df["high_10"] = calc_high_n(h, 10)
    df["high_20"] = calc_high_n(h, 20)
    df["high_60"] = calc_high_n(h, 60)
    df["low_5"] = calc_low_n(l, 5)
    df["low_20"] = calc_low_n(l, 20)
    df["low_60"] = calc_low_n(l, 60)

    # ---- 均线交叉 ----
    df["ma_cross_5_20"] = calc_ma_cross(df["ma5"], df["ma20"])
    df["ma_cross_10_20"] = calc_ma_cross(df["ma10"], df["ma20"])

    # ---- 多头排列 ----
    df["ma_bull_score"] = calc_ma_arrangement(df, [5, 10, 20, 60])

    # ---- ATR ----
    df["atr14"] = calc_atr(h, l, c, 14)
    df["atr_pct"] = df["atr14"] / c * 100

    # ---- 振幅 ----
    df["amplitude"] = calc_amplitude(h, l, c)

    # ---- 影线 ----
    df["lower_shadow"] = calc_lower_shadow(o, c, l)
    df["upper_shadow"] = calc_upper_shadow(o, h, c)

    return df
