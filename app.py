"""Flask Web 应用：ETF扫描 + 持仓管理统一入口"""

import logging
import sys
from datetime import datetime

from flask import Flask, jsonify, render_template, request

from config import ETF_POOL_SIZE
from database import (
    add_position,
    add_to_position,
    get_history,
    get_position_by_id,
    get_positions,
    get_reviews,
    init_db,
    sell_position,
    update_position,
)
from main import fetch_all_etf_data, daily_scan
from position_manager import compute_initial_risk, review_all_positions
from strategy import detect_market_regime

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
        return jsonify({
            "market": {
                "regime": market.regime,
                "score": market.score,
                "description": market.description,
            },
            "signals": [
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
            ],
        })
    except Exception as e:
        logging.exception("扫描失败")
        return jsonify({"error": str(e)}), 500


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
        trailing_activate=risk["trailing_activate"],
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


if __name__ == "__main__":
    app.run(debug=True, port=5000)
