"""持仓复盘模块：每日盘后对已有持仓重新计算指标，生成动态风控建议"""

import logging
from datetime import datetime

import pandas as pd

from config import (
    BUY_THRESHOLD,
    SELL_SCORE_THRESHOLD,
    STOP_LOSS_ATR_MULT,
    STRONG_BUY_THRESHOLD,
    TRAILING_STOP_ATR_MULT,
)
from database import add_review, get_positions
from indicators import calc_all_indicators
from data_provider import fetch_etf_history
from strategy import score_buy_signal

logger = logging.getLogger(__name__)

TIME_STOP_DAYS = 20  # 持仓超过20天且无盈利则触发时间止损


def _infer_ts_code(code: str) -> str:
    """从6位ETF代码推断 Tushare ts_code（.SH/.SZ 后缀）"""
    if code.startswith("5") or code.startswith("0"):
        return f"{code}.SH"
    return f"{code}.SZ"


def review_position(pos: dict) -> dict | None:
    """对单只持仓执行盘后复盘，返回复盘结果 dict（同时写入数据库）"""
    code = pos["code"]
    name = pos["name"]
    ts_code = _infer_ts_code(code)

    result = fetch_etf_history(code, name, ts_code)
    if result is None:
        logger.warning("复盘失败，无法获取数据: %s %s", code, name)
        return None

    _, _, df_raw = result
    if df_raw is None or len(df_raw) < 30:
        logger.warning("复盘失败，数据不足: %s", code)
        return None

    df = calc_all_indicators(df_raw)
    last = df.iloc[-1]

    required = ["ma5", "ma10", "ma20", "ma60", "macd_dif", "macd_dea",
                "macd_hist", "rsi", "boll_mid", "boll_upper", "atr", "vol_ma5"]
    if last[required].isna().any():
        logger.warning("复盘失败，指标含 NaN: %s", code)
        return None

    current_price = float(last["收盘"])
    atr_val = float(last["atr"])
    score, details = score_buy_signal(df)

    buy_date = pos["buy_date"]
    today_str = datetime.now().strftime("%Y-%m-%d")
    holding_days = (
        datetime.strptime(today_str, "%Y-%m-%d")
        - datetime.strptime(buy_date, "%Y-%m-%d")
    ).days

    buy_price = float(pos["buy_price"])
    pnl_pct = round((current_price - buy_price) / buy_price * 100, 2)

    # 持仓期内最高价（用于移动止损判断）
    # 注：Tushare 日期格式为 YYYYMMDD，buy_date 为 YYYY-MM-DD，需统一后再比较
    buy_date_ts = buy_date.replace("-", "")
    df_since_buy = df[df["日期"].astype(str) >= buy_date_ts]
    recent_high = float(df_since_buy["最高"].max()) if not df_since_buy.empty else current_price

    initial_atr = float(pos["initial_atr"])
    stop_loss = float(pos["stop_loss"])
    take_profit = float(pos["take_profit"])
    trailing_activate = float(pos["trailing_activate"])
    trailing_stop_price = recent_high - TRAILING_STOP_ATR_MULT * initial_atr

    # 止损/止盈/移动止损/时间止损 优先级检查
    stop_triggered = "none"
    if current_price <= stop_loss:
        stop_triggered = "stop_loss"
    elif current_price >= take_profit:
        stop_triggered = "take_profit"
    elif recent_high >= trailing_activate and current_price <= trailing_stop_price:
        stop_triggered = "trailing"
    elif holding_days > TIME_STOP_DAYS and pnl_pct <= 0:
        stop_triggered = "time"

    # 建议逻辑
    new_stop_loss = None
    new_take_profit = None

    if stop_triggered != "none":
        recommendation = "clear"
        label = {
            "stop_loss": "止损触及",
            "take_profit": "止盈触及",
            "trailing": "移动止损触发",
            "time": f"持仓{holding_days}天未盈利，时间止损",
        }[stop_triggered]
        reason = f"{label}，建议清仓"

    elif score >= STRONG_BUY_THRESHOLD:
        candidate_sl = round(current_price - TRAILING_STOP_ATR_MULT * atr_val, 3)
        if candidate_sl > stop_loss:
            new_stop_loss = candidate_sl
            recommendation = "move_sl_up"
            reason = f"评分{score}，趋势强劲，建议上移止损至 {new_stop_loss}"
        else:
            recommendation = "hold"
            reason = f"评分{score}，趋势强劲，继续持有"

    elif score <= SELL_SCORE_THRESHOLD:
        recommendation = "clear"
        reason = f"评分{score}跌破卖出阈值{SELL_SCORE_THRESHOLD}，建议清仓"

    elif score < BUY_THRESHOLD:
        candidate_tp = round(current_price + 1.0 * atr_val, 3)
        if candidate_tp < take_profit:
            new_take_profit = candidate_tp
            recommendation = "tighten_tp"
            reason = f"评分{score}，趋势走弱，建议收紧止盈至 {new_take_profit}"
        else:
            recommendation = "reduce"
            reason = f"评分{score}，趋势走弱，建议减仓观察"

    else:
        recommendation = "hold"
        reason = f"评分{score}，结构健康，继续持有"

    review_id = add_review(
        position_id=pos["id"],
        review_date=today_str,
        current_price=current_price,
        current_score=score,
        holding_days=holding_days,
        pnl_pct=pnl_pct,
        stop_triggered=stop_triggered,
        recommendation=recommendation,
        reason=reason,
        new_stop_loss=new_stop_loss,
        new_take_profit=new_take_profit,
        detail_json=details,
    )

    sl_range = buy_price - stop_loss
    tp_range = take_profit - buy_price
    to_stop_pct = round(
        max(0.0, min(100.0, (current_price - stop_loss) / sl_range * 100)) if sl_range > 0 else 100.0, 1
    )
    to_tp_pct = round(
        max(0.0, min(100.0, (current_price - buy_price) / tp_range * 100)) if tp_range > 0 else 0.0, 1
    )

    return {
        "review_id": review_id,
        "position_id": pos["id"],
        "code": code,
        "name": name,
        "current_price": current_price,
        "current_score": score,
        "holding_days": holding_days,
        "pnl_pct": pnl_pct,
        "stop_triggered": stop_triggered,
        "recommendation": recommendation,
        "reason": reason,
        "new_stop_loss": new_stop_loss,
        "new_take_profit": new_take_profit,
        "to_stop_pct": to_stop_pct,
        "to_tp_pct": to_tp_pct,
        "details": details,
    }


def review_all_positions() -> list[dict]:
    """对所有开仓持仓执行盘后复盘"""
    positions = get_positions(status="open")
    results = []
    for pos in positions:
        result = review_position(pos)
        if result is not None:
            results.append(result)
    return results


def compute_initial_risk(code: str, name: str) -> dict | None:
    """新增持仓时，根据最新 ATR 自动计算初始止损/止盈/移动止损启动价"""
    ts_code = _infer_ts_code(code)
    result = fetch_etf_history(code, name, ts_code)
    if result is None:
        return None

    _, _, df_raw = result
    if df_raw is None or len(df_raw) < 30:
        return None

    df = calc_all_indicators(df_raw)
    last = df.iloc[-1]
    if pd.isna(last["atr"]) or pd.isna(last["收盘"]):
        return None

    price = float(last["收盘"])
    atr = float(last["atr"])

    return {
        "current_price": round(price, 3),
        "atr": round(atr, 4),
        "stop_loss": round(price - STOP_LOSS_ATR_MULT * atr, 3),
        "take_profit": round(price + 3.0 * atr, 3),
        "trailing_activate": round(price + 1.0 * atr, 3),
    }
