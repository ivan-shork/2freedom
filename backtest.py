"""回测引擎：日线级别模拟交易，计算绩效指标"""

import logging

import numpy as np
import pandas as pd
from dataclasses import dataclass, field

from config import (
    BEAR_THRESHOLD,
    BULL_THRESHOLD,
    BUY_THRESHOLD,
    INITIAL_CAPITAL,
    MAX_POSITIONS,
    SELL_SCORE_THRESHOLD,
    SINGLE_POSITION_PCT,
    STOP_LOSS_ATR_MULT,
    TAKE_PROFIT_ATR_MULT,
    TRAILING_ACTIVATE_ATR_MULT,
    TRAILING_STOP_ATR_MULT,
)
from indicators import calc_all_indicators
from strategy import MarketRegime, score_buy_signal

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Trade:
    """一笔完整交易记录（不可变）"""
    code: str
    name: str
    entry_date: str
    entry_price: float
    exit_date: str
    exit_price: float
    exit_reason: str        # "take_profit" | "stop_loss" | "trailing_stop" | "signal_sell" | "backtest_end"
    pnl_pct: float


@dataclass
class _Position:
    """持仓（内部可变：跟踪最高价）"""
    code: str
    name: str
    entry_date: str
    entry_price: float
    highest_price: float
    entry_atr: float          # 入场时ATR，用于动态止损止盈
    stop_loss: float          # 止损价 = entry - N×ATR
    take_profit: float        # 止盈价 = entry + M×ATR
    trailing_activate: float  # 移动止损启动价 = entry + P×ATR
    shares: int = 0


@dataclass(frozen=True)
class BacktestResult:
    """回测绩效报告（不可变）"""
    total_return: float
    annual_return: float
    max_drawdown: float
    sharpe_ratio: float
    win_rate: float
    total_trades: int
    avg_holding_days: float
    trades: list[Trade]
    equity_curve: list[float]


class BacktestEngine:
    """日线级别回测引擎

    支持：固定止损、止盈、移动止损、信号卖出
    """

    def __init__(
        self,
        initial_capital: float = INITIAL_CAPITAL,
        stop_loss_mult: float = STOP_LOSS_ATR_MULT,
        take_profit_mult: float = TAKE_PROFIT_ATR_MULT,
        trailing_stop_mult: float = TRAILING_STOP_ATR_MULT,
        trailing_activate_mult: float = TRAILING_ACTIVATE_ATR_MULT,
        buy_threshold: int = BUY_THRESHOLD,
        sell_threshold: int = SELL_SCORE_THRESHOLD,
        max_positions: int = MAX_POSITIONS,
        position_pct: float = SINGLE_POSITION_PCT,
    ) -> None:
        self.initial_capital = initial_capital
        self.stop_loss_mult = stop_loss_mult
        self.take_profit_mult = take_profit_mult
        self.trailing_stop_mult = trailing_stop_mult
        self.trailing_activate_mult = trailing_activate_mult
        self.buy_threshold = buy_threshold
        self.sell_threshold = sell_threshold
        self.max_positions = max_positions
        self.position_pct = position_pct

    def run(
        self,
        etf_data: dict[str, tuple[str, pd.DataFrame]],
        index_df: pd.DataFrame | None = None,
    ) -> BacktestResult:
        """运行回测

        Args:
            etf_data:  {code: (name, df)} — df 需含 日期/开盘/收盘/最高/最低/成交量 列
            index_df:  上证指数历史数据（含 trade_date/close 列），用于动态市场环境判断；
                       不传则全程按 sideways 处理
        """
        regime_map = self._build_regime_map(index_df) if index_df is not None else {}

        # 收集所有交易日
        all_dates: set[str] = set()
        for _, (_, df) in etf_data.items():
            all_dates.update(df["日期"].astype(str).tolist())
        all_dates_sorted = sorted(all_dates)

        if len(all_dates_sorted) < 60:
            logger.warning("回测日期不足60天，无法运行")
            return self._empty_result()

        # 为每只 ETF 建立日期->收盘价 / 开盘价索引
        etf_indexed: dict[str, tuple[str, dict[str, float], dict[str, float], pd.DataFrame]] = {}
        for code, (name, df) in etf_data.items():
            close_map: dict[str, float] = {}
            open_map: dict[str, float] = {}
            for _, row in df.iterrows():
                d = str(row["日期"])
                close_map[d] = float(row["收盘"])
                open_map[d] = float(row["开盘"])
            etf_indexed[code] = (name, close_map, open_map, df)

        capital = self.initial_capital
        positions: list[_Position] = []
        # 待执行买入队列（信号日收盘后入队，次日开盘执行）: (code, name, entry_atr)
        pending_buys: list[tuple[str, str, float]] = []
        closed_trades: list[Trade] = []
        equity_curve: list[float] = [capital]

        test_dates = all_dates_sorted[60:]

        for current_date in test_dates:
            # --- 0. 执行前一日挂单，今日开盘价入场 ---
            executed: set[str] = set()
            for pb_code, pb_name, pb_atr in pending_buys:
                if len(positions) >= self.max_positions:
                    break
                if pb_code in {p.code for p in positions}:
                    continue
                _, _, open_map, _ = etf_indexed[pb_code]
                entry_price = open_map.get(current_date)
                if entry_price is None:
                    continue  # ETF 当日停牌，放弃此笔
                invest = capital * self.position_pct
                shares = int(invest / entry_price / 100) * 100
                if shares <= 0 or capital < shares * entry_price:
                    continue
                capital -= shares * entry_price
                positions.append(_Position(
                    code=pb_code,
                    name=pb_name,
                    entry_date=current_date,
                    entry_price=entry_price,
                    highest_price=entry_price,
                    entry_atr=pb_atr,
                    stop_loss=entry_price - self.stop_loss_mult * pb_atr,
                    take_profit=entry_price + self.take_profit_mult * pb_atr,
                    trailing_activate=entry_price + self.trailing_activate_mult * pb_atr,
                    shares=shares,
                ))
                executed.add(pb_code)
            pending_buys = [pb for pb in pending_buys if pb[0] not in executed]

            # --- 1. 当日市场环境 → 动态买入阈值 ---
            current_regime = regime_map.get(current_date, MarketRegime("sideways", 0.5, "默认"))
            effective_threshold = self._effective_buy_threshold(current_regime.regime)

            # --- 2. 检查持仓：止损 / 止盈 / 移动止损 / 信号卖出 ---
            to_close: list[tuple[_Position, float, str]] = []

            for pos in positions:
                _, close_map, _, _ = etf_indexed.get(pos.code, (None, {}, {}, None))
                price = close_map.get(current_date) if close_map else None
                if price is None:
                    continue

                pos.highest_price = max(pos.highest_price, price)

                if price >= pos.take_profit:
                    to_close.append((pos, price, "take_profit"))
                elif price <= pos.stop_loss:
                    to_close.append((pos, price, "stop_loss"))
                elif (
                    pos.highest_price >= pos.trailing_activate
                    and price <= pos.highest_price - self.trailing_stop_mult * pos.entry_atr
                ):
                    to_close.append((pos, price, "trailing_stop"))
                else:
                    _, _, _, df_raw = etf_indexed[pos.code]
                    hist = df_raw[df_raw["日期"].astype(str) <= current_date].tail(120)
                    if len(hist) >= 61:
                        hist = calc_all_indicators(hist)
                        last = hist.iloc[-1]
                        required = ["ma5", "ma10", "ma20", "ma60", "macd_dif", "macd_dea", "macd_hist", "rsi", "boll_mid", "boll_upper", "boll_lower", "vol_ma5"]
                        if not last[required].isna().any():
                            sig_score, _ = score_buy_signal(hist)
                            if sig_score <= self.sell_threshold:
                                to_close.append((pos, price, "signal_sell"))

            for pos, price, reason in to_close:
                capital += price * pos.shares
                pnl = (price - pos.entry_price) / pos.entry_price
                closed_trades.append(Trade(
                    code=pos.code,
                    name=pos.name,
                    entry_date=pos.entry_date,
                    entry_price=round(pos.entry_price, 3),
                    exit_date=current_date,
                    exit_price=round(price, 3),
                    exit_reason=reason,
                    pnl_pct=round(pnl * 100, 2),
                ))
                if pos in positions:
                    positions.remove(pos)

            # --- 3. 扫描买入信号，挂单次日开盘执行 ---
            pending_codes = {pb[0] for pb in pending_buys}
            if len(positions) + len(pending_buys) < self.max_positions:
                candidates: list[tuple[str, str, int, float]] = []
                held_codes = {p.code for p in positions} | pending_codes

                for code, (name, close_map, _, df_raw) in etf_indexed.items():
                    if code in held_codes:
                        continue

                    hist = df_raw[df_raw["日期"].astype(str) <= current_date].tail(120)
                    if len(hist) < 61:
                        continue

                    hist = calc_all_indicators(hist)
                    last = hist.iloc[-1]
                    required = ["ma5", "ma10", "ma20", "ma60", "macd_dif", "macd_dea", "macd_hist", "rsi", "boll_mid", "boll_upper", "boll_lower", "vol_ma5", "atr"]
                    if last[required].isna().any():
                        continue

                    sig_score, _ = score_buy_signal(hist)
                    if sig_score >= effective_threshold:
                        candidates.append((code, name, sig_score, float(last["atr"])))

                candidates.sort(key=lambda x: x[2], reverse=True)
                slots = self.max_positions - len(positions) - len(pending_buys)
                for code, name, _, atr_val in candidates[:slots]:
                    pending_buys.append((code, name, atr_val))

            # --- 4. 记录每日权益 ---
            position_value = 0.0
            for pos in positions:
                _, close_map, _, _ = etf_indexed.get(pos.code, (None, {}, {}, None))
                p = close_map.get(current_date) if close_map else None
                if p is not None:
                    position_value += p * pos.shares

            equity_curve.append(capital + position_value)

        # --- 5. 回测结束，平掉剩余持仓 ---
        last_date = test_dates[-1]
        for pos in positions:
            _, close_map, _, _ = etf_indexed.get(pos.code, (None, {}, {}, None))
            price = close_map.get(last_date) if close_map else None
            if price is None:
                continue
            capital += price * pos.shares
            pnl = (price - pos.entry_price) / pos.entry_price
            closed_trades.append(Trade(
                code=pos.code,
                name=pos.name,
                entry_date=pos.entry_date,
                entry_price=round(pos.entry_price, 3),
                exit_date=last_date,
                exit_price=round(price, 3),
                exit_reason="backtest_end",
                pnl_pct=round(pnl * 100, 2),
            ))

        return self._calc_metrics(closed_trades, equity_curve)

    def _effective_buy_threshold(self, regime: str) -> int:
        """根据市场环境动态调整买入阈值，与 strategy.determine_action 保持一致"""
        threshold = self.buy_threshold
        if regime == "bear":
            threshold += 15
        elif regime == "sideways":
            threshold += 5
        return threshold

    @staticmethod
    def _build_regime_map(index_df: pd.DataFrame) -> dict[str, MarketRegime]:
        """预计算每个交易日的市场环境（向量化，O(n)）"""
        df = index_df.sort_values("trade_date").reset_index(drop=True)
        ma20 = df["close"].rolling(20).mean()
        ma60 = df["close"].rolling(60).mean()
        ret_20d = df["close"].pct_change(20)

        regime_map: dict[str, MarketRegime] = {}
        for i in range(len(df)):
            date = str(df["trade_date"].iloc[i])
            if pd.isna(ma60.iloc[i]):
                regime_map[date] = MarketRegime("sideways", 0.5, "数据不足")
                continue
            score = 0.5
            if df["close"].iloc[i] > ma20.iloc[i]:
                score += 0.15
            if df["close"].iloc[i] > ma60.iloc[i]:
                score += 0.15
            ret = float(ret_20d.iloc[i]) if not pd.isna(ret_20d.iloc[i]) else 0.0
            score += float(np.clip(ret * 2, -0.3, 0.3))
            score = float(np.clip(score, 0, 1))
            if score >= BULL_THRESHOLD:
                regime = "bull"
            elif score <= BEAR_THRESHOLD:
                regime = "bear"
            else:
                regime = "sideways"
            regime_map[date] = MarketRegime(regime, score, "历史回测")
        return regime_map

    def _calc_metrics(self, trades: list[Trade], equity_curve: list[float]) -> BacktestResult:
        """计算绩效指标"""
        if not trades:
            return self._empty_result()

        total_return = (equity_curve[-1] / self.initial_capital - 1) * 100

        trading_days = len(equity_curve) - 1
        years = max(trading_days / 252, 0.01)
        annual_return = ((1 + total_return / 100) ** (1 / years) - 1) * 100

        peak = equity_curve[0]
        max_dd = 0.0
        for eq in equity_curve:
            peak = max(peak, eq)
            dd = (peak - eq) / peak
            max_dd = max(max_dd, dd)

        returns = pd.Series(equity_curve).pct_change().dropna()
        if len(returns) > 1 and returns.std() > 0:
            sharpe = float((returns.mean() * 252 - 0.03) / (returns.std() * np.sqrt(252)))
        else:
            sharpe = 0.0

        wins = sum(1 for t in trades if t.pnl_pct > 0)
        win_rate = wins / len(trades) * 100

        holding_days: list[float] = []
        for t in trades:
            try:
                d1 = pd.Timestamp(t.entry_date)
                d2 = pd.Timestamp(t.exit_date)
                holding_days.append(float((d2 - d1).days))
            except Exception:
                pass
        avg_days = float(np.mean(holding_days)) if holding_days else 0.0

        return BacktestResult(
            total_return=round(total_return, 2),
            annual_return=round(annual_return, 2),
            max_drawdown=round(max_dd * 100, 2),
            sharpe_ratio=round(sharpe, 2),
            win_rate=round(win_rate, 2),
            total_trades=len(trades),
            avg_holding_days=round(avg_days, 1),
            trades=trades,
            equity_curve=equity_curve,
        )

    def _empty_result(self) -> BacktestResult:
        return BacktestResult(
            total_return=0, annual_return=0, max_drawdown=0,
            sharpe_ratio=0, win_rate=0, total_trades=0,
            avg_holding_days=0, trades=[], equity_curve=[self.initial_capital],
        )
