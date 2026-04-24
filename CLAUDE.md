# 2freedom — A股ETF中短期交易策略

## 项目概述

基于技术指标的A股ETF量化扫描与持仓管理系统。通过 Tushare Pro 接口获取全市场ETF行情，计算技术指标并打分，生成买卖信号；同时提供 Web UI 进行持仓台账管理、每日盘后复盘和交易历史统计。

## 运行方式

```bash
pip install -r requirements.txt

# 首次运行需设置 Tushare token（永久生效，设置后重开终端）
setx TUSHARE_TOKEN 你的token

# 启动 Web 界面（推荐）
python app.py
# 浏览器打开 http://localhost:5000

# CLI 扫描（仅命令行输出，不含持仓管理）
python main.py

# 强制刷新缓存（忽略当日已缓存数据）
python main.py --refresh
```

### 获取 Tushare Token
1. 在 [tushare.pro](https://tushare.pro) 注册账号
2. 完善个人资料可获得 120 积分（满足所有接口权限要求）
3. 在"个人中心"复制 token

## 文件结构

```
config.py            全局配置（评分权重、止损止盈、API参数等）
data_provider.py     数据层（Tushare接口封装、日级别本地缓存）
indicators.py        技术指标计算（MA/MACD/RSI/布林带/ATR）
strategy.py          策略核心（加权评分、市场环境判断、信号生成）
backtest.py          回测引擎（日线模拟、绩效指标计算）
main.py              CLI入口（并发数据获取、扫描、输出）
database.py          SQLite持久层（持仓台账、复盘记录、交易流水）
position_manager.py  持仓复盘算法（每日盘后重算指标、动态风控建议）
app.py               Flask Web服务（ETF扫描 + 持仓管理统一入口）
templates/
  index.html         单页Web UI（Bootstrap 5 + 原生JS，三个Tab）
trading.db           SQLite数据库（自动创建，存储持仓和交易历史）
data_cache/          本地缓存目录（自动创建，当日数据复用）
etf_buy_signal.py    原始版本（保留参考）
requirements.txt     依赖：tushare, pandas, numpy, flask
```

## Web UI 功能（python app.py）

### Tab1 — ETF 扫描
- 显示当前市场环境（牛市/震荡/熊市）及评分
- 一键全量扫描全市场 ETF，展示买入信号列表
- 每个信号显示：代码、名称、评分、当前价、止损价、止盈价
- 扫描结果可直接「+ 录入」到持仓台账

### Tab2 — 持仓管理
- 持仓表格：买入均价、份额、当前价、浮盈亏、距止损进度条（绿）、距止盈进度条（红）、评分、建议
- **手动添加持仓**：填写代码、名称、买入日期、买入价、份额，止损/止盈留空时自动按 ATR 计算
- **补仓**：填写补仓价和份额，自动加权重算买入均价，止损止盈保留不变
- **减仓/平仓**：填写卖出价和份额（留空则全平），份额归零自动标记平仓
- **编辑风控**：手动修改止损价、止盈价（复盘建议不自动写入，需手动确认）
- **一键盘后复盘**：对所有持仓重新计算指标，输出动态调仓建议

### Tab3 — 交易历史
- 按持仓周期（全平后才形成一条记录）展示历史
- 显示：开仓日、平仓日、持仓天数、总份额、买入均价、卖出均价、盈亏金额、盈亏%
- 底部汇总：总交易数、胜率、累计盈亏、平均持仓天数

## 数据库结构（trading.db）

### positions — 持仓台账
| 字段 | 说明 |
|------|------|
| code / name | ETF代码/名称 |
| buy_date / buy_price | 开仓日期 / 当前买入均价（补仓后自动重算） |
| shares | 当前持仓份额 |
| stop_loss / take_profit | 当前止损/止盈价（手动可改） |
| initial_atr | 建仓时ATR快照（用于移动止损） |
| trailing_activate | 移动止损启动价 |
| status | open / closed |
| close_date / close_price | 平仓日期/价格 |

### position_reviews — 每日复盘记录
每次「盘后复盘」写入，含当日评分、浮盈亏、建议操作、建议新止损/止盈。

### trades — 交易流水
每笔买入（含补仓）和卖出单独一行，记录交易时的买入均价快照及每笔卖出盈亏。

## 持仓复盘逻辑（position_manager.py）

每日盘后对所有 `status='open'` 持仓执行：

1. 拉取最新历史数据（走当日缓存，盘后只调用一次 API）
2. `calc_all_indicators()` 重算技术指标
3. `score_buy_signal()` 重新评分
4. 止损检查（优先级由高到低）：
   - 当前价 ≤ 止损价 → `stop_loss`
   - 当前价 ≥ 止盈价 → `take_profit`
   - 持仓期最高价 ≥ 移动止损启动价 且 当前价 ≤ 最高价 - 1.5×ATR → `trailing`
   - 持仓 > 20 天且浮亏 → `time`（时间止损）
5. 建议逻辑：
   - 有触及 → 建议清仓
   - 评分 ≥ 75 且可上移止损 → 建议上移止损
   - 评分 ≤ 40 → 建议清仓
   - 评分 40~65 且可收紧止盈 → 建议收紧止盈
   - 否则 → 持有

## 数据缓存（data_provider.py）

- 同一自然日内，历史数据只从 Tushare 拉取一次，后续全部读本地缓存
- 缓存目录：`data_cache/`（ETF池 JSON + 各标的 pickle + 指数 pickle）
- 缓存有效期：当天日期匹配则命中，次日自动重新拉取
- `USE_CACHE = True`（config.py），可设为 False 禁用
- `python main.py --refresh` 可强制忽略当日缓存

## 策略逻辑

### 数据源
- **Tushare Pro** 接口（替代原 akshare，原因：akshare 依赖东方财富CDN节点，国内IP频繁被限流/拒绝）
  - `fund_basic` + `fund_daily` 获取全市场ETF行情及日成交额
  - `fund_daily`（按 ts_code）获取单只ETF日线历史
  - `index_daily` 获取上证指数日线数据
- 筛选条件：剔除货币/债券/国债/转债/黄金/原油/纳指/日经ETF，日成交额 > 5000万，取流动性前100只
- 历史数据拉取最近 **365个自然日**（约1年交易日，前60天预热指标，剩余约250天为有效回测区间）
- 上证指数拉取最近 **420个自然日**（保证 MA60 在长假后有足够数据）

### Tushare 字段说明
| Tushare 字段 | 单位 | 转换后 | 用途 |
|---|---|---|---|
| `fund_daily.amount` | 千元 | ×1000 → 元 | 成交额筛选 |
| `fund_daily.vol` | 手 | 直接用（相对比较） | 量能信号 |
| `fund_daily.close/open/high/low` | 元/份 | rename | 技术指标 |
| `index_daily.close` | 点 | 直接用 | 市场环境 |

### 技术指标
| 指标 | 用途 |
|------|------|
| MA5/10/20/60 | 趋势方向、均线排列、金叉判断 |
| MACD (DIF/DEA/柱状图) | 中短期动量确认 |
| RSI14 (Wilder's EMA) | 超买超卖判断 |
| 布林带 (中轨/上轨/下轨) | 波动率与支撑位 |
| ATR14 | 波动率衡量，用于动态止损止盈计算 |
| 成交量MA5 | 量能确认 |

### 评分体系（总分100）

5个独立维度，每个维度分层互斥取最高匹配档。

**趋势强度（最高30分）**
| 条件 | 得分 |
|------|------|
| MA5 > MA10 > MA20 > MA60（完全多头） | 30 |
| MA5 > MA10 > MA20（三线多头） | 20 |
| 收盘价 > MA20（仅站上20日线） | 10 |

**MACD动量（最高25分）**
| 条件 | 得分 |
|------|------|
| DIF上穿DEA（金叉当日） | 25 |
| DIF > DEA 且柱状图较前日扩大（动能持续） | 20 |
| DIF > DEA 但柱状图较前日收缩（动能衰减） | 10 |

**RSI区间（最高20分）**
| 条件 | 得分 |
|------|------|
| RSI 45~65（动能健康，最佳入场区） | 20 |
| RSI 30~45（从超卖回暖） | 12 |
| RSI 65~75（强势但接近超买） | 8 |

**量能确认（最高15分）**
| 条件 | 得分 |
|------|------|
| 收盘上涨且成交量 > 5日均量×1.5（强放量） | 15 |
| 收盘上涨且成交量 > 5日均量×1.2（温和放量） | 10 |

**布林带支撑（10分）**
| 条件 | 得分 |
|------|------|
| 价格在中轨之上、上轨之下 | 10 |

### 信号阈值
- **强烈买入**: 评分 >= 75（熊市提高至85）
- **买入观察**: 评分 >= 65（震荡市+5，熊市+15）
- **卖出**: 评分 <= 40

### 市场环境过滤
- 基于上证指数（000001.SH）的MA20/MA60位置和20日动量综合评分
- 牛市（>=0.7）/ 震荡（0.3~0.7）/ 熊市（<=0.3）
- 熊市自动提高买入门槛，减少假突破风险

## 回测引擎（backtest.py）

### 数据范围与预热
- 历史数据365天，前60个交易日用于预热指标（MA60需要60根K线）
- 实际回测区间约250个交易日（约1年），样本量足够统计显著

### 退出机制（基于ATR动态计算）
1. **止盈** 买入价 + 3×ATR
2. **止损** 买入价 - 2×ATR
3. **移动止损** 盈利超过1×ATR后启动，从最高价回撤1.5×ATR
4. **信号卖出** 评分降至40以下

### 仓位管理
- 最大同时持仓 5 只
- 单只仓位不超过总资金 20%
- 整手买入（100股为单位）

### 绩效指标
- 总收益率、年化收益率、最大回撤、夏普比率（无风险利率3%）
- 胜率、总交易次数、平均持仓天数、完整交易记录（含退出原因）

## 配置调整（config.py）

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `HISTORY_DAYS` | 365 | 历史数据拉取天数 |
| `USE_CACHE` | True | 启用日级别本地缓存 |
| `ETF_POOL_SIZE` | 100 | 扫描ETF数量 |
| `STOP_LOSS_ATR_MULT` | 2.0 | 止损ATR倍数 |
| `TAKE_PROFIT_ATR_MULT` | 3.0 | 止盈ATR倍数 |
| `TRAILING_STOP_ATR_MULT` | 1.5 | 移动止损ATR倍数 |
| `BUY_THRESHOLD` | 65 | 买入评分门槛 |
| `STRONG_BUY_THRESHOLD` | 75 | 强烈买入门槛 |
| `SELL_SCORE_THRESHOLD` | 40 | 卖出评分门槛 |
| `MAX_POSITIONS` | 5 | 最大同时持仓数 |
| `INITIAL_CAPITAL` | 100000 | 回测初始资金 |

## Flask API（app.py）

| 方法 | 路径 | 功能 |
|------|------|------|
| GET | `/` | Web UI 主页 |
| GET | `/api/market_regime` | 获取当前市场环境 |
| POST | `/api/scan` | 触发全量ETF扫描 |
| GET | `/api/positions` | 获取持仓列表 |
| POST | `/api/positions` | 新增持仓 |
| PATCH | `/api/positions/<id>` | 修改止损/止盈/备注 |
| POST | `/api/positions/<id>/add` | 补仓（重算均价） |
| POST | `/api/positions/<id>/sell` | 减仓/平仓 |
| POST | `/api/positions/review` | 一键盘后复盘 |
| GET | `/api/positions/<id>/reviews` | 单只持仓复盘历史 |
| GET | `/api/history` | 已平仓交易历史 |

## 依赖

- Python >= 3.10（使用 `X | None` 类型语法）
- tushare（A股数据接口，需注册获取 token）
- pandas / numpy（数据处理）
- flask（Web服务）

## 注意事项

- Tushare 免费账号每分钟限200次调用，100只ETF并发拉取不会触发限流
- 若报"权限不足"，在 tushare.pro 完善个人资料获取积分即可（免费120积分够用）
- `TUSHARE_TOKEN` 通过环境变量注入，不要硬编码在代码里
- 持仓复盘依赖当日收盘后 Tushare 数据更新，建议 15:30 后执行
- 回测基于日线收盘价，不考虑盘中滑点和手续费
- 策略仅供研究参考，不构成投资建议
