"""ETF策略每日扫描主程序

- 并发获取全市场ETF数据
- 判断市场环境（牛/熊/震荡）
- 生成买卖信号
- 回测策略绩效
"""

import argparse
import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

from config import (
    ETF_POOL_SIZE,
    MAX_WORKERS,
)
from data_provider import fetch_etf_history, get_etf_pool, set_refresh
from strategy import (
    Signal,
    MarketRegime,
    detect_market_regime,
    generate_signal,
)
from backtest import BacktestEngine, BacktestResult

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("etf_scan.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


def fetch_all_etf_data(etf_pool: list[dict]) -> dict[str, tuple[str, pd.DataFrame]]:
    """并发获取所有ETF历史数据"""
    etf_data: dict[str, tuple[str, pd.DataFrame]] = {}

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(
                fetch_etf_history, etf["code"], etf["name"], etf["ts_code"]
            ): etf["code"]
            for etf in etf_pool
        }
        for future in as_completed(futures):
            result = future.result()
            if result is not None:
                code, name, df = result
                etf_data[code] = (name, df)

    logger.info("成功获取 %d/%d 只ETF的历史数据", len(etf_data), len(etf_pool))
    return etf_data


def daily_scan(
    etf_data: dict[str, tuple[str, pd.DataFrame]],
    market: MarketRegime,
) -> list[Signal]:
    """对全池ETF进行信号扫描"""
    signals: list[Signal] = []

    for code, (name, df) in etf_data.items():
        signal = generate_signal(code, name, df, market)
        if signal is not None and signal.action in ("strong_buy", "buy"):
            signals.append(signal)

    signals.sort(key=lambda s: s.score, reverse=True)
    return signals


def print_signals(signals: list[Signal], market: MarketRegime) -> None:
    """格式化输出扫描结果"""
    print("\n" + "=" * 80)
    print("ETF 每日策略扫描报告")
    print(f"市场环境: {market.description}")
    print(f"扫描结果: {len(signals)} 只ETF触发买入信号")
    print("=" * 80)

    if not signals:
        print("今日无买入信号")
        return

    for s in signals:
        label = "强烈买入" if s.action == "strong_buy" else "买入观察"
        sl_pct = (s.stop_loss_price - s.price) / s.price * 100
        tp_pct = (s.take_profit_price - s.price) / s.price * 100
        ta_pct = (s.trailing_activate_price - s.price) / s.price * 100
        print(f"\n  {s.code} | {s.name} | 评分: {s.score} | 价格: {s.price} | ATR: {s.atr} | {label}")
        print(f"    止损: {s.stop_loss_price} ({sl_pct:+.1f}%)  止盈: {s.take_profit_price} ({tp_pct:+.1f}%)  移动止损启动: {s.trailing_activate_price} ({ta_pct:+.1f}%)")
        detail_str = "  ".join(f"{k}={v}" for k, v in s.details.items())
        print(f"    详情: {detail_str}")


def print_backtest_result(result: BacktestResult) -> None:
    """格式化输出回测绩效"""
    print("\n" + "=" * 80)
    print("回测绩效报告")
    print("=" * 80)
    print(f"  总收益率:     {result.total_return:.2f}%")
    print(f"  年化收益率:   {result.annual_return:.2f}%")
    print(f"  最大回撤:     {result.max_drawdown:.2f}%")
    print(f"  夏普比率:     {result.sharpe_ratio:.2f}")
    print(f"  胜率:         {result.win_rate:.2f}%")
    print(f"  总交易次数:   {result.total_trades}")
    print(f"  平均持仓天数: {result.avg_holding_days:.1f}")

    if result.trades:
        reason_labels = {
            "take_profit": "止盈",
            "stop_loss": "止损",
            "trailing_stop": "移动止损",
            "signal_sell": "信号卖出",
            "backtest_end": "回测结束",
        }
        print(f"\n  --- 最近10笔交易 ---")
        for t in result.trades[-10:]:
            reason = reason_labels.get(t.exit_reason, t.exit_reason)
            print(
                f"  {t.code} {t.name} | "
                f"买入:{t.entry_price}({t.entry_date}) -> "
                f"卖出:{t.exit_price}({t.exit_date}) | "
                f"{reason} | 收益:{t.pnl_pct:+.2f}%"
            )


def main() -> None:
    parser = argparse.ArgumentParser(description="A股ETF中短期交易策略扫描")
    parser.add_argument(
        "--refresh", action="store_true", help="强制刷新缓存，重新从API拉取数据"
    )
    args = parser.parse_args()
    if args.refresh:
        set_refresh(True)
        logger.info("已启用强制刷新模式，将忽略本地缓存")

    logger.info("=== 开始每日ETF策略扫描 ===")

    # 1. 市场环境
    market = detect_market_regime()
    logger.info("市场环境: %s", market.description)

    # 2. ETF池
    etf_pool = get_etf_pool()

    # 3. 并发获取历史数据
    etf_data = fetch_all_etf_data(etf_pool)

    # 4. 信号扫描
    signals = daily_scan(etf_data, market)
    print_signals(signals, market)

    # 5. 回测
    logger.info("开始回测...")
    engine = BacktestEngine()
    result = engine.run(etf_data, market)
    print_backtest_result(result)

    logger.info("=== 扫描完成 ===")


if __name__ == "__main__":
    main()
