"""ETF策略每日扫描主程序

- 并发获取全市场ETF数据
- 判断市场环境（牛/熊/震荡）
- 生成买卖信号
- 回测策略绩效
"""

import logging
import re
import sys
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

import akshare as ak
import pandas as pd
import requests as _requests

from config import (
    API_RETRY,
    ETF_EXCLUDE_KEYWORDS,
    ETF_MIN_DAILY_VOLUME,
    ETF_MIN_MARKET_CAP,
    ETF_POOL_SIZE,
    HISTORY_DAYS,
    MAX_WORKERS,
)
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

# 东方财富API的浏览器请求头
_EM_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://quote.eastmoney.com/",
}


def _fetch_etf_spot_direct() -> pd.DataFrame:
    """直接从东方财富API获取全市场ETF实时行情（绕过akshare的CDN节点问题）

    当 ak.fund_etf_spot_em() 因网络问题失败时使用此备用方案。
    """
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    fields = "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f12,f13,f14,f15,f16,f17,f18,f20,f21,f23,f24,f25"
    all_data: list[dict] = []
    page = 1
    per_page = 5000

    while True:
        params = {
            "pn": page, "pz": per_page, "po": "1", "np": "1",
            "ut": "bd1d9ddb04089700cf9c27f6f7426281", "fltt": "2", "invt": "2",
            "fid": "f12",
            "fs": "b:MK0021,b:MK0022,b:MK0023,b:MK0024",
            "fields": fields,
        }
        resp = _requests.get(url, params=params, headers=_EM_HEADERS, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        if data.get("data") is None or data["data"].get("diff") is None:
            break

        items = data["data"]["diff"]
        all_data.extend(items)

        total = data["data"].get("total", 0)
        if len(all_data) >= total:
            break
        page += 1

    # 字段映射：东方财富f编码 -> 中文列名（与akshare fund_etf_spot_em 对齐）
    col_map = {
        "f12": "代码", "f14": "名称", "f2": "最新价", "f3": "涨跌幅",
        "f4": "涨跌额", "f5": "成交量", "f6": "成交额", "f15": "最高价",
        "f16": "最低价", "f17": "开盘价", "f18": "昨收", "f20": "总市值",
        "f21": "流通市值", "f23": "市净率", "f24": "换手率",
    }
    df = pd.DataFrame(all_data)
    df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})

    # 过滤无效数据
    df = df[df["代码"].str.match(r"^\d{6}$")]
    for col in ["成交额", "总市值", "最新价"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    return df


def get_etf_pool(top_n: int = ETF_POOL_SIZE) -> list[dict]:
    """获取优质流动性ETF池，返回 [{code, name}]"""
    logger.info("正在获取全市场ETF行情...")

    try:
        df = ak.fund_etf_spot_em()
        logger.info("通过akshare获取ETF数据成功")
    except Exception as e:
        logger.warning("akshare获取失败(%s)，切换到直接API备用方案...", e)
        df = _fetch_etf_spot_direct()
        logger.info("通过直接API获取ETF数据成功，共 %d 只", len(df))

    exclude_pattern = "|".join(ETF_EXCLUDE_KEYWORDS)
    df = df[
        (~df["名称"].str.contains(exclude_pattern))
        & (df["总市值"] > ETF_MIN_MARKET_CAP)
        & (df["成交额"] > ETF_MIN_DAILY_VOLUME)
    ].copy()

    df = df.sort_values("成交额", ascending=False).head(top_n)

    pool = df[["代码", "名称"]].rename(columns={"代码": "code", "名称": "name"}).to_dict("records")
    logger.info("筛选出 %d 只优质流动性ETF", len(pool))
    return pool


def fetch_etf_history(code: str, name: str) -> tuple[str, str, pd.DataFrame] | None:
    """获取单只ETF历史数据，带重试"""
    start_date = (datetime.now() - timedelta(days=HISTORY_DAYS)).strftime("%Y%m%d")

    for attempt in range(API_RETRY):
        try:
            df = ak.fund_etf_hist_em(symbol=code, period="daily", start_date=start_date)
            if df is None or len(df) < 30:
                logger.debug("%s %s 数据不足30条，跳过", code, name)
                return None
            return code, name, df
        except Exception as e:
            logger.warning("%s %s 第%d次获取失败: %s", code, name, attempt + 1, e)

    logger.error("%s %s 获取历史数据失败，已跳过", code, name)
    return None


def fetch_all_etf_data(etf_pool: list[dict]) -> dict[str, tuple[str, pd.DataFrame]]:
    """并发获取所有ETF历史数据"""
    etf_data: dict[str, tuple[str, pd.DataFrame]] = {}

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(fetch_etf_history, etf["code"], etf["name"]): etf["code"]
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
        print(f"\n  {s.code} | {s.name} | 评分: {s.score} | 价格: {s.price} | {label}")
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
