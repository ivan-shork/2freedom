"""ETF策略全局配置参数"""

# ======================== ETF池筛选 ========================
ETF_MIN_MARKET_CAP: float = 2e8       # 最小市值 2亿
ETF_MIN_DAILY_VOLUME: float = 5e7     # 最小日成交额 5000万
ETF_POOL_SIZE: int = 100              # 每日扫描ETF数量
ETF_EXCLUDE_KEYWORDS: list[str] = [
    "货币", "债券", "国债", "转债", "黄金", "原油", "纳指", "日经",
]
HISTORY_DAYS: int = 120               # 拉取历史天数（约半年交易日）

# ======================== 技术指标参数 ========================
MA_PERIODS: list[int] = [5, 10, 20, 60]
MACD_FAST: int = 12
MACD_SLOW: int = 26
MACD_SIGNAL: int = 9
RSI_PERIOD: int = 14
BOLL_PERIOD: int = 20
BOLL_STD_DEV: float = 2.0
ATR_PERIOD: int = 14

# ======================== 评分权重（总分100） ========================
SCORE_WEIGHTS: dict[str, int] = {
    "trend_ma20": 15,          # 站上20日线
    "ma_bullish": 15,          # 均线多头排列（5>10>20）
    "ma_golden_cross": 15,     # MA5上穿MA10
    "macd_golden_cross": 20,   # MACD金叉（权重最高）
    "rsi_healthy": 15,         # RSI在健康区间（30-65）
    "volume_surge": 10,        # 放量上涨（量比>1.2）
    "boll_support": 10,        # 布林带中轨之上、上轨之下
}

# ======================== 信号阈值 ========================
STRONG_BUY_THRESHOLD: int = 75
BUY_THRESHOLD: int = 60
SELL_SCORE_THRESHOLD: int = 40

# ======================== 风险管理（基于ATR动态计算） ========================
STOP_LOSS_ATR_MULT: float = 2.0        # 止损：买入价 - 2×ATR
TAKE_PROFIT_ATR_MULT: float = 3.0      # 止盈：买入价 + 3×ATR
TRAILING_STOP_ATR_MULT: float = 1.5    # 移动止损：最高价 - 1.5×ATR
TRAILING_ACTIVATE_ATR_MULT: float = 1.0  # 移动止损启动：盈利超过1×ATR后激活
MAX_POSITIONS: int = 5                  # 最大同时持仓数
SINGLE_POSITION_PCT: float = 0.20      # 单只最大仓位 20%

# ======================== 市场环境 ========================
BULL_THRESHOLD: float = 0.7
BEAR_THRESHOLD: float = 0.3

# ======================== API并发 ========================
MAX_WORKERS: int = 10
API_RETRY: int = 3
API_TIMEOUT: int = 30

# ======================== 回测 ========================
INITIAL_CAPITAL: float = 100000.0      # 初始资金10万
