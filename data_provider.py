"""Tushare数据提供层

替代akshare，提供ETF池、历史行情、指数数据接口。
输出列名与原akshare版本保持一致，下游代码无需修改。

支持本地缓存：同一自然日内历史数据不变，缓存后跳过API调用。
前复权通过 fund_adj 接口获取复权因子，公式：price × adj_factor / latest_adj_factor。
"""

import json
import logging
import os
import pickle
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import tushare as ts

from config import (
    API_RETRY,
    ETF_EXCLUDE_KEYWORDS,
    ETF_MIN_DAILY_VOLUME,
    ETF_POOL_SIZE,
    HISTORY_DAYS,
    USE_CACHE,
)

logger = logging.getLogger(__name__)

_pro = None
_force_refresh: bool = False

_CACHE_DIR = Path(__file__).parent / "data_cache"


def set_refresh(refresh: bool) -> None:
    """设置是否强制刷新缓存（由 main.py 的 --refresh 参数调用）"""
    global _force_refresh
    _force_refresh = refresh


# ======================== 缓存工具函数 ========================


def _cache_dir() -> Path:
    """返回缓存根目录，不存在则创建"""
    return _CACHE_DIR


def _is_cache_valid() -> bool:
    """判断缓存是否有效：cache_info.json 存在且 created_date == 今天"""
    if _force_refresh or not USE_CACHE:
        return False
    info_path = _cache_dir() / "cache_info.json"
    if not info_path.exists():
        return False
    try:
        with open(info_path, encoding="utf-8") as f:
            info = json.load(f)
        return info.get("created_date") == datetime.now().strftime("%Y-%m-%d")
    except (json.JSONDecodeError, KeyError):
        return False


def _save_cache_info(trade_date: str) -> None:
    """写入缓存元信息"""
    _cache_dir().mkdir(parents=True, exist_ok=True)
    info = {
        "created_date": datetime.now().strftime("%Y-%m-%d"),
        "trade_date": trade_date,
    }
    with open(_cache_dir() / "cache_info.json", "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=2)


def _load_pool_cache() -> list[dict] | None:
    """从缓存读取ETF池"""
    if not _is_cache_valid():
        return None
    path = _cache_dir() / "pool.json"
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            pool = json.load(f)
        logger.info("使用缓存数据 (ETF池)")
        return pool
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("读取ETF池缓存失败: %s", e)
        return None


def _save_pool_cache(pool: list[dict], trade_date: str) -> None:
    """将ETF池写入缓存"""
    _cache_dir().mkdir(parents=True, exist_ok=True)
    with open(_cache_dir() / "pool.json", "w", encoding="utf-8") as f:
        json.dump(pool, f, ensure_ascii=False, indent=2)
    _save_cache_info(trade_date)


def _load_etf_cache(ts_code: str) -> pd.DataFrame | None:
    """从缓存读取单只ETF历史数据"""
    if not _is_cache_valid():
        return None
    path = _cache_dir() / "etf" / f"{ts_code}.pkl"
    if not path.exists():
        return None
    try:
        with open(path, "rb") as f:
            df = pickle.load(f)
        return df
    except (pickle.UnpicklingError, OSError) as e:
        logger.warning("读取ETF缓存失败 %s: %s", ts_code, e)
        return None


def _save_etf_cache(ts_code: str, df: pd.DataFrame) -> None:
    """将单只ETF历史数据写入缓存"""
    etf_dir = _cache_dir() / "etf"
    etf_dir.mkdir(parents=True, exist_ok=True)
    with open(etf_dir / f"{ts_code}.pkl", "wb") as f:
        pickle.dump(df, f)


def _load_index_cache(ts_code: str) -> pd.DataFrame | None:
    """从缓存读取指数日线数据"""
    if not _is_cache_valid():
        return None
    path = _cache_dir() / "index" / f"{ts_code}.pkl"
    if not path.exists():
        return None
    try:
        with open(path, "rb") as f:
            df = pickle.load(f)
        return df
    except (pickle.UnpicklingError, OSError) as e:
        logger.warning("读取指数缓存失败 %s: %s", ts_code, e)
        return None


def _save_index_cache(ts_code: str, df: pd.DataFrame) -> None:
    """将指数日线数据写入缓存"""
    index_dir = _cache_dir() / "index"
    index_dir.mkdir(parents=True, exist_ok=True)
    with open(index_dir / f"{ts_code}.pkl", "wb") as f:
        pickle.dump(df, f)


# ======================== API 函数 ========================


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
    # 缓存命中直接返回
    cached = _load_pool_cache()
    if cached is not None:
        return cached

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

    # 写入缓存
    _save_pool_cache(pool, trade_date)
    return pool


def lookup_etf_info(code: str) -> tuple[str, str] | None:
    """通过6位代码查找ETF的正确 ts_code 和名称。
    优先查当日池子缓存，未命中则调用 fund_basic 接口。
    返回 (ts_code, name)，找不到返回 None。
    """
    # 先查当日池子缓存（今天扫描过则命中）
    cached_pool = _load_pool_cache()
    if cached_pool:
        for item in cached_pool:
            if item["code"] == code:
                return item["ts_code"], item["name"]

    # 池子里没有，调 fund_basic 查全量列表
    try:
        pro = _get_pro()
        df = pro.fund_basic(market="E", status="L")[["ts_code", "name"]]
        df["code"] = df["ts_code"].str[:6]
        match = df[df["code"] == code]
        if not match.empty:
            row = match.iloc[0]
            return str(row["ts_code"]), str(row["name"])
    except Exception as e:
        logger.warning("查询ETF信息失败 %s: %s", code, e)

    return None



def _apply_qfq(df: pd.DataFrame, adj_df: pd.DataFrame) -> pd.DataFrame:
    """用 fund_adj 复权因子做前复权。

    公式: qfq_price = original_price * adj_factor / latest_adj_factor
    成交量不调整。
    """
    if adj_df is None or adj_df.empty:
        return df
    df = df.copy()
    adj_map = dict(zip(adj_df["trade_date"], adj_df["adj_factor"]))
    df["adj_factor"] = df["日期"].map(adj_map)
    if df["adj_factor"].isna().any():
        df["adj_factor"] = df["adj_factor"].ffill().bfill()
    latest_factor = df["adj_factor"].iloc[-1]
    ratio = df["adj_factor"] / latest_factor
    for col in ["开盘", "最高", "最低", "收盘"]:
        df[col] = df[col] * ratio
    df = df.drop(columns=["adj_factor"])
    return df


def fetch_etf_history(
    code: str, name: str, ts_code: str
) -> tuple[str, str, pd.DataFrame] | None:
    """获取单只ETF历史日线数据，带重试。

    返回 DataFrame 列名：日期 / 开盘 / 最高 / 最低 / 收盘 / 成交量
    价格已通过 fund_adj 复权因子做前复权处理。
    """
    # 缓存命中直接返回
    cached_df = _load_etf_cache(ts_code)
    if cached_df is not None:
        return code, name, cached_df

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

            # 获取复权因子并做前复权
            try:
                adj_df = pro.fund_adj(
                    ts_code=ts_code,
                    start_date=start_date,
                    end_date=end_date,
                )
                df = _apply_qfq(df, adj_df)
            except Exception as adj_err:
                logger.warning(
                    "%s %s 获取复权因子失败，使用未复权数据: %s",
                    code, name, adj_err,
                )

            # 写入缓存
            _save_etf_cache(ts_code, df)
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
    # 缓存命中直接返回
    cached_df = _load_index_cache(ts_code)
    if cached_df is not None:
        return cached_df

    pro = _get_pro()
    start_date = (datetime.now() - timedelta(days=420)).strftime("%Y%m%d")
    df = pro.index_daily(
        ts_code=ts_code,
        start_date=start_date,
        fields="trade_date,close",
    )
    if df is None or df.empty:
        return pd.DataFrame()

    result = df.sort_values("trade_date").reset_index(drop=True)

    # 写入缓存
    _save_index_cache(ts_code, result)
    return result
