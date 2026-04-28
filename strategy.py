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
    """计算买入评分 0~100，返回 (分数, 各项详情)

    评分体系（5个独立维度，总分100）：
      趋势强度(30) + MACD动量(25) + RSI区间(20) + 量能(15) + 布林带(10)
    """
    last = df.iloc[-1]
    prev = df.iloc[-2]
    prev2 = df.iloc[-3]
    score = 0
    details: dict[str, str] = {}

    # --- 趋势强度（最高30分，分层互斥，取最高匹配档） ---
    if last["ma5"] > last["ma10"] > last["ma20"] and last["ma20"] > last["ma60"]:
        pts = SCORE_WEIGHTS["trend_strength"]   # 30
        details["trend_strength"] = f"+{pts}(完全多头5>10>20>60)"
    elif last["ma5"] > last["ma10"] > last["ma20"]:
        pts = 20
        details["trend_strength"] = "+20(三线多头5>10>20)"
    elif last["收盘"] > last["ma20"]:
        pts = 10
        details["trend_strength"] = "+10(站上MA20)"
    else:
        pts = 0
        details["trend_strength"] = "0"
    score += pts

    # --- MACD动量（最高25分，分层互斥） ---
    # 判断动能结构而非单日金叉事件，避免假金叉噪声
    dif_above_dea = last["macd_dif"] > last["macd_dea"]
    dif_above_zero = last["macd_dif"] > 0
    hist_expanding_today = last["macd_hist"] > prev["macd_hist"]
    hist_expanding_2days = hist_expanding_today and prev["macd_hist"] > prev2["macd_hist"]

    if dif_above_dea and dif_above_zero and hist_expanding_2days:
        pts = SCORE_WEIGHTS["macd_momentum"]    # 25：零轴上方连续2日扩张，结构最强
        details["macd_momentum"] = f"+{pts}(DIF>0且柱连续扩张)"
    elif dif_above_dea and dif_above_zero and hist_expanding_today:
        pts = 18                                # 18：零轴上方单日扩张，待次日确认
        details["macd_momentum"] = "+18(DIF>0且柱今日扩张)"
    elif dif_above_dea and dif_above_zero:
        pts = 10                                # 10：零轴上方但动能衰减
        details["macd_momentum"] = "+10(DIF>0柱收缩)"
    elif dif_above_dea and hist_expanding_today:
        pts = 10                                # 10：零轴下方反弹，谨慎
        details["macd_momentum"] = "+10(DIF<0但柱扩张)"
    else:
        pts = 0
        details["macd_momentum"] = "0"
    score += pts

    # --- RSI区间（最高20分，分层互斥） ---
    # 结合方向判断：同一区间内，RSI上升与下降含义截然不同
    rsi_val = float(last.get("rsi", 50))
    rsi_rising = last["rsi"] > prev["rsi"]

    if 45 <= rsi_val < 65 and rsi_rising:
        pts = SCORE_WEIGHTS["rsi_zone"]         # 20：健康区间且动能正在建立
        details["rsi_zone"] = f"+{pts}(RSI动能建立={rsi_val:.1f}↑)"
    elif 30 <= rsi_val < 50 and rsi_rising:
        pts = 12                                # 12：从超卖回暖，方向已确认
        details["rsi_zone"] = f"+12(RSI回暖确认={rsi_val:.1f}↑)"
    elif 65 <= rsi_val < 75:
        pts = 8                                 # 8：强势区，接近超买不区分方向
        details["rsi_zone"] = f"+8(RSI强势区={rsi_val:.1f})"
    elif 45 <= rsi_val < 65:
        pts = 8                                 # 8：健康区间但动能在消耗
        details["rsi_zone"] = f"+8(RSI健康区下行={rsi_val:.1f}↓)"
    else:
        pts = 0
        details["rsi_zone"] = f"0(RSI={rsi_val:.1f})"
    score += pts

    # --- 量能确认（最高15分，五象限量价分析） ---
    price_change_pct = (last["收盘"] - prev["收盘"]) / prev["收盘"]
    price_up = last["收盘"] > prev["收盘"]       # 布林带维度复用
    price_up_eff = price_change_pct > 0.003      # 有效上涨：涨幅 > 0.3%
    price_stagnant = abs(price_change_pct) <= 0.003  # 滞涨/滞跌区间
    price_down_eff = price_change_pct < -0.003   # 有效下跌：跌幅 > 0.3%
    vol_ratio = last["成交量"] / last["vol_ma5"] if last["vol_ma5"] > 0 else 0.0

    if price_up_eff and vol_ratio >= 1.5:
        pts = SCORE_WEIGHTS["volume_surge"]      # 15：放量有效上涨，最强确认
        details["volume_surge"] = f"+{pts}(放量上涨×{vol_ratio:.1f})"
    elif price_up_eff and vol_ratio >= 1.2:
        pts = 10                                 # 10：温和放量上涨
        details["volume_surge"] = f"+10(温和放量×{vol_ratio:.1f})"
    elif price_down_eff and vol_ratio < 0.8:
        pts = 8                                  # 8：缩量回调，抛压枯竭
        details["volume_surge"] = f"+8(缩量回调×{vol_ratio:.1f})"
    elif price_up_eff:
        pts = 5                                  # 5：平量有效上涨
        details["volume_surge"] = f"+5(平量上涨×{vol_ratio:.1f})"
    elif price_stagnant and vol_ratio >= 1.2:
        pts = 0                                  # 0：放量滞涨，量价背离疑似出货
        details["volume_surge"] = f"0(放量滞涨×{vol_ratio:.1f}警示)"
    else:
        pts = 0
        details["volume_surge"] = f"0(量比×{vol_ratio:.1f})"
    score += pts

    # --- 布林带支撑（最高10分） ---
    # 用 %B 精确定位价格在布林带内的位置，补充下轨反弹场景
    boll_range = last["boll_upper"] - last["boll_lower"]
    pct_b = (last["收盘"] - last["boll_lower"]) / boll_range if boll_range > 0 else 0.5

    if 0.5 <= pct_b < 0.8 and price_up:
        pts = SCORE_WEIGHTS["boll_support"]     # 10：中轨上方强势区间，动能延续
        details["boll_support"] = f"+{pts}(%B={pct_b:.2f}强势区间)"
    elif 0.0 <= pct_b < 0.2 and price_up:
        pts = 8                                 # 8：下轨附近反弹，支撑确认
        details["boll_support"] = f"+8(%B={pct_b:.2f}下轨反弹)"
    elif 0.8 <= pct_b <= 1.0:
        pts = 5                                 # 5：接近上轨，强势但注意压力
        details["boll_support"] = f"+5(%B={pct_b:.2f}接近上轨)"
    else:
        pts = 0
        details["boll_support"] = f"0(%B={pct_b:.2f})"
    score += pts

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
    if len(df) < 61:
        logger.debug("%s %s 数据不足60条，跳过", code, name)
        return None

    df = calc_all_indicators(df)
    last = df.iloc[-1]

    required = ["ma5", "ma10", "ma20", "ma60", "macd_dif", "macd_dea", "macd_hist", "rsi", "boll_mid", "boll_upper", "boll_lower", "vol_ma5"]
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
