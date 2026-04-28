"""Flask Web 应用：ETF扫描 + 持仓管理统一入口"""

import logging
import sys
from datetime import datetime

from flask import Flask, jsonify, render_template, request

from config import ETF_POOL_SIZE, STOP_LOSS_ATR_MULT, TAKE_PROFIT_ATR_MULT, TRAILING_ACTIVATE_ATR_MULT
from database import (
    add_position,
    add_to_position,
    delete_position,
    get_history,
    get_position_by_id,
    get_positions,
    get_reviews,
    get_scan_dates,
    get_scan_signals_by_date,
    init_db,
    save_scan_signals,
    sell_position,
    update_position,
)
from indicators import calc_all_indicators
from backtest import BacktestEngine
from main import fetch_all_etf_data, daily_scan
from position_manager import compute_initial_risk, review_all_positions
from strategy import detect_market_regime, score_buy_signal, determine_action
from data_provider import fetch_etf_history, lookup_etf_info

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

app = Flask(__name__)
init_db()


# ── 页面 ──────────────────────────────────────────────────

@app.get("/")
def index():
    return render_template("index.html")


# ── Tab1：ETF 扫描 ────────────────────────────────────────

@app.get("/api/market_regime")
def api_market_regime():
    try:
        regime = detect_market_regime()
        return jsonify({"regime": regime.regime, "score": regime.score, "description": regime.description})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.post("/api/scan")
def api_scan():
    try:
        market = detect_market_regime()
        from data_provider import get_etf_pool
        etf_pool = get_etf_pool(ETF_POOL_SIZE)
        etf_data = fetch_all_etf_data(etf_pool)
        signals = daily_scan(etf_data, market)
        signal_list = [
            {
                "code": s.code,
                "name": s.name,
                "score": s.score,
                "price": s.price,
                "action": s.action,
                "atr": s.atr,
                "stop_loss_price": s.stop_loss_price,
                "take_profit_price": s.take_profit_price,
                "trailing_activate_price": s.trailing_activate_price,
                "details": s.details,
            }
            for s in signals
        ]
        scan_date = datetime.now().strftime("%Y-%m-%d")
        save_scan_signals(scan_date, market.regime, market.score, signal_list)
        return jsonify({
            "market": {
                "regime": market.regime,
                "score": market.score,
                "description": market.description,
            },
            "signals": signal_list,
        })
    except Exception as e:
        logging.exception("扫描失败")
        return jsonify({"error": str(e)}), 500


@app.get("/api/scan_history/dates")
def api_scan_history_dates():
    return jsonify(get_scan_dates())


@app.get("/api/scan_history")
def api_scan_history():
    scan_date = request.args.get("date")
    if not scan_date:
        return jsonify({"error": "缺少 date 参数"}), 400
    return jsonify(get_scan_signals_by_date(scan_date))


# ── Tab2：持仓管理 ────────────────────────────────────────

@app.get("/api/positions")
def api_get_positions():
    status = request.args.get("status", "open")
    return jsonify(get_positions(status))


@app.post("/api/positions")
def api_add_position():
    data = request.get_json(force=True)
    required = ["code", "name", "buy_date", "buy_price", "shares"]
    if missing := [f for f in required if f not in data]:
        return jsonify({"error": f"缺少字段: {missing}"}), 400

    code = data["code"].strip()
    name = data["name"].strip()
    buy_price = float(data["buy_price"])
    shares = int(data["shares"])

    risk = compute_initial_risk(code, name)
    if risk is None:
        return jsonify({"error": "无法获取该ETF数据，请检查代码是否正确"}), 400

    stop_loss = float(data.get("stop_loss") or risk["stop_loss"])
    take_profit = float(data.get("take_profit") or risk["take_profit"])

    pos_id = add_position(
        code=code,
        name=name,
        buy_date=data["buy_date"],
        buy_price=buy_price,
        shares=shares,
        stop_loss=stop_loss,
        take_profit=take_profit,
        initial_atr=risk["atr"],
        trailing_activate=round(buy_price + risk["atr"], 3),
        notes=data.get("notes", ""),
    )
    return jsonify({"id": pos_id, "risk": risk}), 201


@app.patch("/api/positions/<int:pos_id>")
def api_update_position(pos_id: int):
    data = request.get_json(force=True)
    fields = {k: v for k, v in data.items() if k in ("stop_loss", "take_profit", "notes")}
    if not fields:
        return jsonify({"error": "无可更新字段"}), 400
    ok = update_position(pos_id, **fields)
    return jsonify({"ok": ok})


@app.delete("/api/positions/<int:pos_id>")
def api_delete_position(pos_id: int):
    """删除持仓（误录入场景，同时删除关联复盘和流水）"""
    ok = delete_position(pos_id)
    if not ok:
        return jsonify({"error": "持仓不存在"}), 404
    return jsonify({"ok": True})


@app.post("/api/positions/<int:pos_id>/add")
def api_add_to_position(pos_id: int):
    """补仓"""
    data = request.get_json(force=True)
    try:
        result = add_to_position(
            position_id=pos_id,
            price=float(data["price"]),
            shares=int(data["shares"]),
            trade_date=data.get("trade_date") or datetime.now().strftime("%Y-%m-%d"),
        )
        return jsonify(result)
    except (ValueError, KeyError) as e:
        return jsonify({"error": str(e)}), 400


@app.post("/api/positions/<int:pos_id>/sell")
def api_sell_position(pos_id: int):
    """减仓或全平"""
    data = request.get_json(force=True)
    pos = get_position_by_id(pos_id)
    if pos is None:
        return jsonify({"error": "持仓不存在"}), 404
    try:
        shares = int(data.get("shares") or pos["shares"])  # 默认全平
        result = sell_position(
            position_id=pos_id,
            price=float(data["price"]),
            shares=shares,
            trade_date=data.get("trade_date") or datetime.now().strftime("%Y-%m-%d"),
        )
        return jsonify(result)
    except (ValueError, KeyError) as e:
        return jsonify({"error": str(e)}), 400


@app.post("/api/positions/review")
def api_review():
    try:
        results = review_all_positions()
        return jsonify(results)
    except Exception as e:
        logging.exception("复盘失败")
        return jsonify({"error": str(e)}), 500


@app.get("/api/positions/<int:pos_id>/reviews")
def api_get_reviews(pos_id: int):
    limit = int(request.args.get("limit", 30))
    return jsonify(get_reviews(pos_id, limit))


# ── Tab3：交易历史 ────────────────────────────────────────

@app.get("/api/history")
def api_history():
    return jsonify(get_history())


# ── Tab6：策略回测 ───────────────────────────────────────

@app.post("/api/backtest")
def api_backtest():
    try:
        body = request.get_json(silent=True) or {}
        # 日期格式统一转为 YYYYMMDD；前端传 YYYY-MM-DD
        def _fmt(d: str | None) -> str | None:
            return d.replace("-", "") if d else None

        start_date = _fmt(body.get("start_date"))
        end_date = _fmt(body.get("end_date"))

        # 指数需向前多取90天保证 MA60 预热
        from datetime import datetime as _dt, timedelta as _td
        index_start = None
        if start_date:
            index_start = (_dt.strptime(start_date, "%Y%m%d") - _td(days=90)).strftime("%Y%m%d")

        from data_provider import get_etf_pool, get_index_daily
        etf_pool = get_etf_pool(ETF_POOL_SIZE)
        etf_data = fetch_all_etf_data(etf_pool, start_date, end_date)
        index_df = get_index_daily("000001.SH", index_start, end_date)
        result = BacktestEngine().run(etf_data, index_df)
        return jsonify({
            "metrics": {
                "total_return":    result.total_return,
                "annual_return":   result.annual_return,
                "max_drawdown":    result.max_drawdown,
                "sharpe_ratio":    result.sharpe_ratio,
                "win_rate":        result.win_rate,
                "total_trades":    result.total_trades,
                "avg_holding_days":result.avg_holding_days,
            },
            "trades": [
                {
                    "code":        t.code,
                    "name":        t.name,
                    "entry_date":  t.entry_date,
                    "entry_price": t.entry_price,
                    "exit_date":   t.exit_date,
                    "exit_price":  t.exit_price,
                    "exit_reason": t.exit_reason,
                    "pnl_pct":     t.pnl_pct,
                }
                for t in result.trades
            ],
            "equity_curve": result.equity_curve,
        })
    except Exception as e:
        logging.exception("回测失败")
        return jsonify({"error": str(e)}), 500


# ── Tab5：单只ETF分析 ────────────────────────────────────

@app.get("/api/analyze")
def api_analyze():
    code = (request.args.get("code") or "").strip()
    if not code:
        return jsonify({"error": "缺少 code 参数"}), 400

    # 优先用前端传入的名称（从持仓点击过来时有值）
    name = (request.args.get("name") or "").strip()
    date_str = (request.args.get("date") or "").strip()

    # 通过 Tushare 验证 ts_code 和名称，避免 _infer_ts_code 猜错交易所后缀
    etf_info = lookup_etf_info(code)
    if etf_info is None:
        return jsonify({"error": f"找不到代码 {code} 对应的ETF，请确认代码正确"}), 400
    ts_code, fetched_name = etf_info
    if not name:
        name = fetched_name

    result = fetch_etf_history(code, name, ts_code)
    if result is None:
        return jsonify({"error": f"无法获取 {code} 历史数据"}), 400

    _, _, df_raw = result
    if df_raw is None or len(df_raw) < 30:
        return jsonify({"error": f"{code} 历史数据不足，无法分析"}), 400

    if date_str:
        date_ts = date_str.replace("-", "")
        df_raw = df_raw[df_raw["日期"].astype(str) <= date_ts]
        if len(df_raw) < 30:
            return jsonify({"error": f"{code} 在 {date_str} 前数据不足（少于30条），无法分析"}), 400

    df = calc_all_indicators(df_raw)
    last = df.iloc[-1]

    required = ["ma5", "ma10", "ma20", "ma60", "macd_dif", "macd_dea",
                "macd_hist", "rsi", "boll_mid", "boll_upper", "boll_lower", "atr", "vol_ma5"]
    if last[required].isna().any():
        return jsonify({"error": f"{code} 指标计算含 NaN，数据可能不足"}), 400

    score, details = score_buy_signal(df)
    market = detect_market_regime()
    action = determine_action(score, market)

    price = float(last["收盘"])
    atr = float(last["atr"])
    vol = float(last["成交量"])
    vol_ma5 = float(last["vol_ma5"])

    return jsonify({
        "code": code,
        "name": name,
        "score": score,
        "action": action,
        "details": details,
        "market": {
            "regime": market.regime,
            "score": market.score,
            "description": market.description,
        },
        "indicators": {
            "price": round(price, 3),
            "atr": round(atr, 4),
            "ma5":  round(float(last["ma5"]),  3),
            "ma10": round(float(last["ma10"]), 3),
            "ma20": round(float(last["ma20"]), 3),
            "ma60": round(float(last["ma60"]), 3),
            "macd_dif":  round(float(last["macd_dif"]),  4),
            "macd_dea":  round(float(last["macd_dea"]),  4),
            "macd_hist": round(float(last["macd_hist"]), 4),
            "rsi": round(float(last["rsi"]), 2),
            "boll_upper": round(float(last["boll_upper"]), 3),
            "boll_mid":   round(float(last["boll_mid"]),   3),
            "boll_lower": round(float(last["boll_lower"]), 3),
            "vol_ratio": round(vol / vol_ma5, 2) if vol_ma5 > 0 else None,
        },
        "risk": {
            "stop_loss":        round(price - STOP_LOSS_ATR_MULT    * atr, 3),
            "take_profit":      round(price + TAKE_PROFIT_ATR_MULT  * atr, 3),
            "trailing_activate":round(price + TRAILING_ACTIVATE_ATR_MULT * atr, 3),
        },
    })


if __name__ == "__main__":
    app.run(debug=True, port=5000)
