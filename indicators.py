"""技术指标计算模块

修复：RSI使用Wilder's EMA替代错误的SMA
新增：布林带、ATR
"""

import numpy as np
import pandas as pd

from config import (
    ATR_PERIOD,
    BOLL_PERIOD,
    BOLL_STD_DEV,
    MACD_FAST,
    MACD_SIGNAL,
    MACD_SLOW,
    MA_PERIODS,
    RSI_PERIOD,
)


def calc_ma(series: pd.Series, periods: list[int] | None = None) -> pd.DataFrame:
    """计算多条移动平均线"""
    if periods is None:
        periods = MA_PERIODS
    return pd.DataFrame({f"ma{p}": series.rolling(p).mean() for p in periods})


def calc_ema(series: pd.Series, span: int) -> pd.Series:
    """指数移动平均"""
    return series.ewm(span=span, adjust=False).mean()


def calc_macd(
    series: pd.Series,
    fast: int = MACD_FAST,
    slow: int = MACD_SLOW,
    signal: int = MACD_SIGNAL,
) -> pd.DataFrame:
    """MACD指标，返回 DIF / DEA / 柱状图"""
    ema_fast = calc_ema(series, fast)
    ema_slow = calc_ema(series, slow)
    dif = ema_fast - ema_slow
    dea = calc_ema(dif, signal)
    return pd.DataFrame({
        "macd_dif": dif,
        "macd_dea": dea,
        "macd_hist": (dif - dea) * 2,
    })


def calc_rsi(series: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    """RSI — Wilder's EMA 方法（修正原脚本SMA错误）

    Wilder 使用 alpha=1/period 的指数平滑，而非简单移动平均。
    """
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calc_boll(
    series: pd.Series,
    period: int = BOLL_PERIOD,
    std_mult: float = BOLL_STD_DEV,
) -> pd.DataFrame:
    """布林带：中轨 / 上轨 / 下轨"""
    mid = series.rolling(period).mean()
    std = series.rolling(period).std()
    return pd.DataFrame({
        "boll_mid": mid,
        "boll_upper": mid + std_mult * std,
        "boll_lower": mid - std_mult * std,
    })


def calc_atr(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = ATR_PERIOD,
) -> pd.Series:
    """ATR（Average True Range），衡量波动率"""
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def calc_volume_ma(volume: pd.Series, period: int = 5) -> pd.Series:
    """成交量均线"""
    return volume.rolling(period).mean()


def calc_all_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """一次性计算全部技术指标，返回新 DataFrame（不修改原数据）"""
    result = df.copy()

    for col_name, series in calc_ma(result["收盘"]).items():
        result[col_name] = series

    for col_name, series in calc_macd(result["收盘"]).items():
        result[col_name] = series

    result["rsi"] = calc_rsi(result["收盘"])

    for col_name, series in calc_boll(result["收盘"]).items():
        result[col_name] = series

    result["atr"] = calc_atr(result["最高"], result["最低"], result["收盘"])
    result["vol_ma5"] = calc_volume_ma(result["成交量"])

    return result
