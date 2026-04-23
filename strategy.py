"""交易策略：加权评分、市场环境判断、买卖信号生成"""

import logging

import numpy as np
import pandas as pd
from dataclasses import dataclass

from config import (
    BEAR_THRESHOLD,
    BULL_THRESHOLD,
    BUY_THRESHOLD,
    SCORE_WEIGHTS,
    SELL_SCORE_THRESHOLD,
    STOP_LOSS_ATR_MULT,
    STRONG_BUY_THRESHOLD,
    TAKE_PROFIT_ATR_MULT,
    TRAILING_ACTIVATE_ATR_MULT,
    TRAILING_STOP_ATR_MULT,
)
from data_provider import get_index_daily
from indicators import calc_all_indicators

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Signal:
    """单只ETF的交易信号（不可变）"""
    code: str
    name: str
    score: int
    price: float
    action: str           # "strong_buy" | "buy" | "hold" | "sell"
    market_regime: str    # "bull" | "bear" | "sideways"
    details: dict
    # ATR动态止损止盈
    atr: float
    stop_loss_price: float
    take_profit_price: float
    trailing_activate_price: float


@dataclass(frozen=True)
class MarketRegime:
    """市场环境判断结果（不可变）"""
    regime: str           # "bull" | "bear" | "sideways"
    score: float          # 0~1，越高越偏牛
    description: str


def detect_market_regime() -> MarketRegime:
    """基于上证指数判断当前市场环境"""
    try:
        df = get_index_daily("000001.SH")
        if df is None or len(df) < 60:
            return MarketRegime("sideways", 0.5, "数据不足，默认震荡")

        close = df["close"].iloc[-1]
        ma20 = df["close"].rolling(20).mean().iloc[-1]
        ma60 = df["close"].rolling(60).mean().iloc[-1]

        # 20日涨幅作为动量指标
        ret_20d = (close / df["close"].iloc[-20] - 1) if len(df) >= 20 else 0.0

        # 综合评分 0~1
        score = 0.5
        if close > ma20:
            score += 0.15
        if close > ma60:
            score += 0.15
        score += float(np.clip(ret_20d * 2, -0.3, 0.3))
        score = float(np.clip(score, 0, 1))

        if score >= BULL_THRESHOLD:
            regime = "bull"
        elif score <= BEAR_THRESHOLD:
            regime = "bear"
        else:
            regime = "sideways"

        desc = (
            f"{'牛市' if regime == 'bull' else '熊市' if regime == 'bear' else '震荡'}"
            f" (score={score:.2f}, 站上MA20={close > ma20}, 站上MA60={close > ma60})"
        )
        return MarketRegime(regime, score, desc)

    except Exception as e:
        logger.warning("市场环境判断失败: %s，默认震荡", e)
        return MarketRegime("sideways", 0.5, f"判断失败({e})，默认震荡")


def score_buy_signal(df: pd.DataFrame) -> tuple[int, dict]:
    """计算买入评分 0~100，返回 (分数, 各项详情)"""
    last = df.iloc[-1]
    prev = df.iloc[-2]
    score = 0
    details: dict[str, str] = {}

    def _add(key: str, condition: bool) -> None:
        nonlocal score
        w = SCORE_WEIGHTS[key]
        if condition:
            score += w
            details[key] = f"+{w}"
        else:
            details[key] = "0"

    # 站上20日线
    _add("trend_ma20", last["收盘"] > last["ma20"])

    # 均线多头排列 5 > 10 > 20
    _add("ma_bullish", last["ma5"] > last["ma10"] > last["ma20"])

    # MA5 上穿 MA10（金叉）
    _add("ma_golden_cross", prev["ma5"] <= prev["ma10"] and last["ma5"] > last["ma10"])

    # MACD 金叉
    _add("macd_golden_cross", prev["macd_dif"] <= prev["macd_dea"] and last["macd_dif"] > last["macd_dea"])

    # RSI 健康区间（不超买不超卖）
    rsi_val = float(last.get("rsi", 50))
    is_rsi_healthy = 30 < rsi_val < 65
    _add("rsi_healthy", is_rsi_healthy)
    details["rsi_healthy"] = f"{details['rsi_healthy']} (RSI={rsi_val:.1f})"

    # 放量上涨
    _add("volume_surge", last["收盘"] > prev["收盘"] and last["成交量"] > last["vol_ma5"] * 1.2)

    # 布林带中轨支撑
    _add("boll_support", last["收盘"] > last["boll_mid"] and last["收盘"] < last["boll_upper"])

    return min(score, 100), details


def determine_action(score: int, market: MarketRegime) -> str:
    """综合评分和市场环境决定操作方向"""
    buy_threshold = BUY_THRESHOLD
    strong_threshold = STRONG_BUY_THRESHOLD

    if market.regime == "bear":
        buy_threshold += 15
        strong_threshold += 10
    elif market.regime == "sideways":
        buy_threshold += 5

    if score >= strong_threshold:
        return "strong_buy"
    if score >= buy_threshold:
        return "buy"
    if score <= SELL_SCORE_THRESHOLD:
        return "sell"
    return "hold"


def generate_signal(
    code: str,
    name: str,
    df: pd.DataFrame,
    market: MarketRegime,
) -> Signal | None:
    """为单只ETF生成交易信号"""
    if len(df) < 60:
        logger.debug("%s %s 数据不足60条，跳过", code, name)
        return None

    df = calc_all_indicators(df)
    last = df.iloc[-1]

    required = ["ma5", "ma10", "ma20", "macd_dif", "macd_dea", "rsi", "boll_mid", "boll_upper", "vol_ma5"]
    if last[required].isna().any():
        logger.debug("%s %s 指标存在NaN，跳过", code, name)
        return None

    score, details = score_buy_signal(df)
    action = determine_action(score, market)

    # ATR动态止损止盈
    atr_val = float(last["atr"])
    entry_price = float(last["收盘"])

    return Signal(
        code=code,
        name=name,
        score=score,
        price=round(entry_price, 3),
        action=action,
        market_regime=market.regime,
        details=details,
        atr=round(atr_val, 4),
        stop_loss_price=round(entry_price - STOP_LOSS_ATR_MULT * atr_val, 3),
        take_profit_price=round(entry_price + TAKE_PROFIT_ATR_MULT * atr_val, 3),
        trailing_activate_price=round(entry_price + TRAILING_ACTIVATE_ATR_MULT * atr_val, 3),
    )
