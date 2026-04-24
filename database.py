"""SQLite 持久层：持仓台账、每日复盘记录、交易流水"""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

_DB_PATH = Path(__file__).parent / "trading.db"

_DDL = """
CREATE TABLE IF NOT EXISTS positions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    code              TEXT    NOT NULL,
    name              TEXT    NOT NULL,
    buy_date          TEXT    NOT NULL,
    buy_price         REAL    NOT NULL,
    shares            INTEGER NOT NULL,
    stop_loss         REAL    NOT NULL,
    take_profit       REAL    NOT NULL,
    initial_atr       REAL    NOT NULL,
    trailing_activate REAL    NOT NULL,
    status            TEXT    NOT NULL DEFAULT 'open',
    close_date        TEXT,
    close_price       REAL,
    notes             TEXT,
    created_at        TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at        TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS position_reviews (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id     INTEGER NOT NULL REFERENCES positions(id),
    review_date     TEXT    NOT NULL,
    current_price   REAL    NOT NULL,
    current_score   INTEGER NOT NULL,
    holding_days    INTEGER NOT NULL,
    pnl_pct         REAL    NOT NULL,
    stop_triggered  TEXT    NOT NULL,
    recommendation  TEXT    NOT NULL,
    reason          TEXT    NOT NULL,
    new_stop_loss   REAL,
    new_take_profit REAL,
    detail_json     TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id     INTEGER NOT NULL REFERENCES positions(id),
    trade_date      TEXT    NOT NULL,
    trade_type      TEXT    NOT NULL,   -- 'buy' | 'sell'
    price           REAL    NOT NULL,
    shares          INTEGER NOT NULL,
    avg_buy_price   REAL    NOT NULL,   -- 交易发生时的买入均价快照
    pnl             REAL,               -- 仅 sell 有值：(price - avg_buy_price) * shares
    pnl_pct         REAL,               -- 仅 sell 有值：盈亏百分比
    notes           TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
CREATE INDEX IF NOT EXISTS idx_reviews_position ON position_reviews(position_id);
CREATE INDEX IF NOT EXISTS idx_reviews_date     ON position_reviews(review_date);
CREATE INDEX IF NOT EXISTS idx_trades_position  ON trades(position_id);
"""


@contextmanager
def _conn():
    con = sqlite3.connect(_DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def init_db() -> None:
    with _conn() as con:
        con.executescript(_DDL)


# =================== positions ===================

def add_position(
    code: str,
    name: str,
    buy_date: str,
    buy_price: float,
    shares: int,
    stop_loss: float,
    take_profit: float,
    initial_atr: float,
    trailing_activate: float,
    notes: str = "",
) -> int:
    sql_pos = """
        INSERT INTO positions
            (code, name, buy_date, buy_price, shares,
             stop_loss, take_profit, initial_atr, trailing_activate, notes)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """
    with _conn() as con:
        cur = con.execute(
            sql_pos,
            (code, name, buy_date, buy_price, shares,
             stop_loss, take_profit, initial_atr, trailing_activate, notes),
        )
        pos_id = cur.lastrowid
        con.execute(
            "INSERT INTO trades (position_id, trade_date, trade_type, price, shares, avg_buy_price) VALUES (?,?,?,?,?,?)",
            (pos_id, buy_date, "buy", buy_price, shares, buy_price),
        )
        return pos_id


def get_positions(status: str = "open") -> list[dict]:
    sql = """
        SELECT p.*,
               r.current_score,
               r.recommendation,
               r.reason,
               r.new_stop_loss,
               r.new_take_profit,
               r.pnl_pct,
               r.current_price,
               r.review_date
        FROM positions p
        LEFT JOIN position_reviews r
          ON r.id = (
              SELECT id FROM position_reviews
              WHERE position_id = p.id
              ORDER BY created_at DESC
              LIMIT 1
          )
        WHERE p.status = ?
        ORDER BY p.created_at DESC
    """
    with _conn() as con:
        rows = con.execute(sql, (status,)).fetchall()
    return [dict(r) for r in rows]


def get_position_by_id(position_id: int) -> dict | None:
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM positions WHERE id = ?", (position_id,)
        ).fetchone()
    return dict(row) if row else None


def update_position(position_id: int, **fields) -> bool:
    allowed = {"stop_loss", "take_profit", "notes"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    set_clause += ", updated_at = datetime('now','localtime')"
    sql = f"UPDATE positions SET {set_clause} WHERE id = ?"
    with _conn() as con:
        cur = con.execute(sql, (*updates.values(), position_id))
    return cur.rowcount > 0


def add_to_position(position_id: int, price: float, shares: int, trade_date: str) -> dict:
    """补仓：加权重算买入均价，记录买入流水"""
    pos = get_position_by_id(position_id)
    if pos is None:
        raise ValueError(f"持仓不存在: {position_id}")

    old_shares = pos["shares"]
    old_avg = pos["buy_price"]
    new_shares = old_shares + shares
    new_avg = round((old_avg * old_shares + price * shares) / new_shares, 4)

    with _conn() as con:
        con.execute(
            "UPDATE positions SET shares=?, buy_price=?, updated_at=datetime('now','localtime') WHERE id=?",
            (new_shares, new_avg, position_id),
        )
        con.execute(
            "INSERT INTO trades (position_id, trade_date, trade_type, price, shares, avg_buy_price) VALUES (?,?,?,?,?,?)",
            (position_id, trade_date, "buy", price, shares, new_avg),
        )
    return {"new_shares": new_shares, "new_avg_buy_price": new_avg}


def sell_position(position_id: int, price: float, shares: int, trade_date: str) -> dict:
    """减仓/平仓：减少份额，记录卖出流水，份额归零时自动平仓"""
    pos = get_position_by_id(position_id)
    if pos is None:
        raise ValueError(f"持仓不存在: {position_id}")

    old_shares = pos["shares"]
    if shares > old_shares:
        raise ValueError(f"卖出份额({shares})超过持仓份额({old_shares})")

    avg_buy = pos["buy_price"]
    new_shares = old_shares - shares
    pnl = round((price - avg_buy) * shares, 2)
    pnl_pct = round((price - avg_buy) / avg_buy * 100, 2)

    with _conn() as con:
        if new_shares == 0:
            con.execute(
                """UPDATE positions SET shares=0, status='closed',
                   close_date=?, close_price=?,
                   updated_at=datetime('now','localtime') WHERE id=?""",
                (trade_date, price, position_id),
            )
        else:
            con.execute(
                "UPDATE positions SET shares=?, updated_at=datetime('now','localtime') WHERE id=?",
                (new_shares, position_id),
            )
        con.execute(
            """INSERT INTO trades
               (position_id, trade_date, trade_type, price, shares, avg_buy_price, pnl, pnl_pct)
               VALUES (?,?,?,?,?,?,?,?)""",
            (position_id, trade_date, "sell", price, shares, avg_buy, pnl, pnl_pct),
        )

    return {
        "remaining_shares": new_shares,
        "pnl": pnl,
        "pnl_pct": pnl_pct,
        "closed": new_shares == 0,
    }


def get_history() -> list[dict]:
    """已平仓持仓的交易历史，按持仓周期汇总"""
    sql = """
        SELECT
            p.id,
            p.code,
            p.name,
            p.buy_date,
            p.close_date,
            p.buy_price          AS avg_buy_price,
            p.shares             AS remaining_shares,
            SUM(CASE WHEN t.trade_type='buy'  THEN t.shares ELSE 0 END) AS total_bought,
            SUM(CASE WHEN t.trade_type='sell' THEN t.shares ELSE 0 END) AS total_sold,
            SUM(CASE WHEN t.trade_type='sell' THEN t.pnl    ELSE 0 END) AS total_pnl,
            CASE
                WHEN SUM(CASE WHEN t.trade_type='sell' THEN t.shares ELSE 0 END) > 0
                THEN ROUND(
                    SUM(CASE WHEN t.trade_type='sell' THEN t.price * t.shares ELSE 0 END) /
                    SUM(CASE WHEN t.trade_type='sell' THEN t.shares ELSE 0 END), 4)
                ELSE NULL
            END AS avg_sell_price
        FROM positions p
        LEFT JOIN trades t ON t.position_id = p.id
        WHERE p.status = 'closed'
        GROUP BY p.id
        ORDER BY p.close_date DESC
    """
    with _conn() as con:
        rows = con.execute(sql).fetchall()
    results = []
    for r in rows:
        row = dict(r)
        if row["buy_date"] and row["close_date"]:
            from datetime import datetime
            d1 = datetime.strptime(row["buy_date"],   "%Y-%m-%d")
            d2 = datetime.strptime(row["close_date"], "%Y-%m-%d")
            row["holding_days"] = (d2 - d1).days
        else:
            row["holding_days"] = None
        cost = row["avg_buy_price"] * row["total_sold"] if row["total_sold"] else None
        row["total_pnl_pct"] = round(row["total_pnl"] / cost * 100, 2) if cost else None
        results.append(row)
    return results


# =================== position_reviews ===================

def add_review(
    position_id: int,
    review_date: str,
    current_price: float,
    current_score: int,
    holding_days: int,
    pnl_pct: float,
    stop_triggered: str,
    recommendation: str,
    reason: str,
    new_stop_loss: float | None,
    new_take_profit: float | None,
    detail_json: dict | None,
) -> int:
    sql = """
        INSERT INTO position_reviews
            (position_id, review_date, current_price, current_score,
             holding_days, pnl_pct, stop_triggered, recommendation,
             reason, new_stop_loss, new_take_profit, detail_json)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
    """
    with _conn() as con:
        cur = con.execute(
            sql,
            (
                position_id, review_date, current_price, current_score,
                holding_days, pnl_pct, stop_triggered, recommendation,
                reason, new_stop_loss, new_take_profit,
                json.dumps(detail_json, ensure_ascii=False) if detail_json else None,
            ),
        )
        return cur.lastrowid


def get_reviews(position_id: int, limit: int = 30) -> list[dict]:
    sql = """
        SELECT * FROM position_reviews
        WHERE position_id = ?
        ORDER BY created_at DESC
        LIMIT ?
    """
    with _conn() as con:
        rows = con.execute(sql, (position_id, limit)).fetchall()
    return [dict(r) for r in rows]
