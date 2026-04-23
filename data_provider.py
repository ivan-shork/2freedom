"""Tushare数据提供层

替代akshare，提供ETF池、历史行情、指数数据接口。
输出列名与原akshare版本保持一致，下游代码无需修改。
"""

import logging
import os
import time
from datetime import datetime, timedelta

import pandas as pd
import tushare as ts

from config import (
    API_RETRY,
    ETF_EXCLUDE_KEYWORDS,
    ETF_MIN_DAILY_VOLUME,
    ETF_POOL_SIZE,
    HISTORY_DAYS,
)

logger = logging.getLogger(__name__)

_pro = None


def _get_pro() -> ts.pro_api:
    global _pro
    if _pro is None:
        token = os.environ.get("TUSHARE_TOKEN", "")
        if not token:
            raise ValueError(
                "未找到 TUSHARE_TOKEN 环境变量。\n"
                "请在 tushare.pro 注册后执行: set TUSHARE_TOKEN=你的token"
            )
        ts.set_token(token)
        _pro = ts.pro_api()
        logger.info("Tushare Pro API 初始化成功")
    return _pro


def _latest_trade_date() -> str:
    """找最近一个有数据的交易日（最多往前找7天）"""
    pro = _get_pro()
    for delta in range(7):
        d = (datetime.now() - timedelta(days=delta)).strftime("%Y%m%d")
        df = pro.fund_daily(trade_date=d, fields="ts_code,amount")
        if df is not None and len(df) > 100:
            return d
    raise RuntimeError("最近7天内未找到有效交易日数据")


def get_etf_pool(top_n: int = ETF_POOL_SIZE) -> list[dict]:
    """获取优质流动性ETF池，返回 [{code, name, ts_code}]"""
    pro = _get_pro()
    logger.info("正在通过Tushare获取全市场ETF行情...")

    # 全量ETF基础信息
    fund_df = pro.fund_basic(market="E", status="L")[["ts_code", "name"]]

    # 最新交易日数据
    # Tushare fund_daily: amount 单位为千元
    trade_date = _latest_trade_date()
    daily_df = pro.fund_daily(trade_date=trade_date, fields="ts_code,amount")
    daily_df["成交额"] = daily_df["amount"] * 1000  # 千元 → 元

    df = fund_df.merge(daily_df[["ts_code", "成交额"]], on="ts_code", how="inner")
    df["代码"] = df["ts_code"].str[:6]
    df["名称"] = df["name"]

    exclude_pattern = "|".join(ETF_EXCLUDE_KEYWORDS)
    # 注：Tushare fund_daily 无总市值字段，用成交额 > 5000万替代市值筛选
    # （成交额 > 5000万的 ETF，AUM 实践上远超 2亿，市值筛选已被覆盖）
    df = df[
        (~df["名称"].str.contains(exclude_pattern, na=False))
        & (df["成交额"] > ETF_MIN_DAILY_VOLUME)
    ].copy()

    df = df.sort_values("成交额", ascending=False).head(top_n)
    pool = (
        df[["代码", "名称", "ts_code"]]
        .rename(columns={"代码": "code", "名称": "name"})
        .to_dict("records")
    )
    logger.info("筛选出 %d 只优质流动性ETF（交易日: %s）", len(pool), trade_date)
    return pool


def fetch_etf_history(
    code: str, name: str, ts_code: str
) -> tuple[str, str, pd.DataFrame] | None:
    """获取单只ETF历史日线数据，带重试。

    返回 DataFrame 列名：日期 / 开盘 / 最高 / 最低 / 收盘 / 成交量
    """
    pro = _get_pro()
    start_date = (datetime.now() - timedelta(days=HISTORY_DAYS)).strftime("%Y%m%d")
    end_date = datetime.now().strftime("%Y%m%d")

    for attempt in range(API_RETRY):
        try:
            df = pro.fund_daily(
                ts_code=ts_code,
                start_date=start_date,
                end_date=end_date,
                fields="trade_date,open,high,low,close,vol",
            )
            if df is None or len(df) < 30:
                logger.debug("%s %s 数据不足30条，跳过", code, name)
                return None

            df = (
                df.sort_values("trade_date")
                .reset_index(drop=True)
                .rename(columns={
                    "trade_date": "日期",
                    "open": "开盘",
                    "high": "最高",
                    "low": "最低",
                    "close": "收盘",
                    "vol": "成交量",
                })
            )
            for col in ["开盘", "最高", "最低", "收盘", "成交量"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            df = df.dropna(subset=["收盘"])

            if len(df) < 30:
                return None
            return code, name, df

        except Exception as e:
            logger.warning("%s %s 第%d次获取失败: %s", code, name, attempt + 1, e)
            if attempt < API_RETRY - 1:
                time.sleep(0.5 * (attempt + 1))

    logger.error("%s %s 获取历史数据失败，已跳过", code, name)
    return None


def get_index_daily(ts_code: str = "000001.SH") -> pd.DataFrame:
    """获取指数日线数据（默认上证指数）。

    返回 DataFrame 含 close 列，按日期升序排列。
    拉取150个自然日（约105个交易日）确保 MA60 有足够数据，同时覆盖国庆/春节长假。
    """
    pro = _get_pro()
    start_date = (datetime.now() - timedelta(days=150)).strftime("%Y%m%d")
    df = pro.index_daily(
        ts_code=ts_code,
        start_date=start_date,
        fields="trade_date,close",
    )
    if df is None or df.empty:
        return pd.DataFrame()
    return df.sort_values("trade_date").reset_index(drop=True)
