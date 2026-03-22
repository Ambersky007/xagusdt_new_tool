# -*- coding: utf-8 -*-
"""
币安期货多周期趋势+剃头皮交易策略
核心功能：
1. 多周期K线数据采集（1m/5m/15m/30m/1h）
2. 实时WebSocket行情接收 + HTTP兜底更新
3. 技术指标计算（MACD/ATR/KDJ/EMA）
4. 双周期ATR波动过滤
5. 震荡区间识别与剃头皮交易
6. 趋势追单（缩量+放量+MACD金叉/死叉）
7. 语音播报与自动下单
8. 主动平仓（MACD反转/EMA破位）
"""

# ===============================
# 导入所需的核心库
# ===============================
import requests          # 发送HTTP请求（获取K线、价格、盘口等数据）
import websocket         # 建立WebSocket长连接（实时接收行情）
import json              # 解析JSON格式的行情数据
import threading         # 多线程（WebSocket独立线程运行）
import time              # 时间控制（延时、时间戳）
import pandas as pd      # 数据处理（K线数据转为DataFrame计算指标）
import numpy as np       # 数值计算（处理ATR、MACD等指标）
from collections import deque  # 高效缓存（固定长度的K线缓存）

# 外部模块导入 - 修复：将has_position改为get_position
from order_executor import place_order, check_stop_orders, get_position  # 下单/止盈止损/持仓查询
from config_account import DEFAULT_ORDER_QTY, SYMBOL  # 账户配置（默认下单量、交易对）
from voice_alert import speak  # 语音播报（下单/平仓提醒）
from price_center import update_ws_price, get_price  # 价格缓存（更新/获取最新价格）
from time_sync import sync_time  # 时间同步（解决API签名时间差问题）

# ===============================
# 配置区 - 策略核心参数配置
# ===============================
SYMBOL = "XAGUSDT"       # 交易对（白银USDT永续合约）
WS_SYMBOL = SYMBOL.lower()  # WebSocket订阅用小写格式

# API基础地址配置（币安期货REST API地址）
HTTP_BASE = "https://fapi.binance.com"

# WebSocket多流订阅地址 - 修复：订阅所有需要的K线周期
# 同时订阅：成交数据、标记价格、深度盘口、多周期K线
WS_URL = (
    f"wss://fstream.binance.com/stream?streams="
    f"{WS_SYMBOL}@aggTrade/"          # 实时成交数据
    f"{WS_SYMBOL}@markPrice/"        # 标记价格（期货核心价格）
    f"{WS_SYMBOL}@depth20/"          # 20档盘口深度
    f"{WS_SYMBOL}@kline_1m/"         # 1分钟K线
    f"{WS_SYMBOL}@kline_5m/"         # 5分钟K线
    f"{WS_SYMBOL}@kline_15m/"        # 15分钟K线
    f"{WS_SYMBOL}@kline_30m/"        # 30分钟K线
    f"{WS_SYMBOL}@kline_1h"          # 1小时K线
)

# 策略监控的K线周期（核心分析周期）
INTERVALS = ["1m", "5m", "15m", "30m", "1h"]
INIT_LIMIT = 120  # 初始化K线时获取的历史数据条数

# WebSocket兜底配置（防止WS断连导致行情中断）
WS_TIMEOUT_SECONDS = 10  # WS无消息超时阈值（秒）
last_ws_message_ts = 0   # 最后一次WS消息的时间戳

# 防重复下单配置（避免短时间内重复触发同一信号）
ORDER_COOLDOWN = 60      # 下单冷却时间（秒）
last_signal = None       # 上一次触发的交易信号
last_order_time = 0      # 上一次下单的时间戳

# ===============================
# 数据缓存区（线程安全的全局数据存储）
# ===============================
cache = {
    "depth": {"bid": None, "ask": None},  # 盘口缓存：买一/卖一价格
    "klines": {i: deque(maxlen=200) for i in INTERVALS},  # 各周期K线缓存（最多200条）
}

lock = threading.Lock()  # 线程锁（防止多线程同时修改cache导致数据错乱）
ws_alive = False         # WebSocket连接状态标记


# ===============================
# 初始化K线数据（程序启动时加载历史K线）
# ===============================
def init_klines():
    print("📥 初始化K线数据...")
    for interval in INTERVALS:  # 遍历所有需要的K线周期
        try:
            # 币安期货K线接口地址
            url = f"{HTTP_BASE}/fapi/v1/klines"
            # 请求参数：交易对、周期、获取条数
            params = {"symbol": SYMBOL, "interval": interval, "limit": INIT_LIMIT}
            # 发送HTTP GET请求获取K线数据
            response = requests.get(url, params=params, timeout=10)
            data = response.json()  # 解析JSON响应

            # 加锁修改缓存（线程安全）
            with lock:
                cache["klines"][interval].clear()  # 清空旧数据
                # 遍历K线数据，转换格式并存入缓存
                for k in data:
                    cache["klines"][interval].append({
                        "time": k[0],      # K线开始时间戳（毫秒）
                        "open": float(k[1]),# 开盘价
                        "high": float(k[2]),# 最高价
                        "low": float(k[3]), # 最低价
                        "close": float(k[4]),# 收盘价
                        "volume": float(k[5])# 成交量
                    })
            print(f"✅ {interval} K线初始化成功")
        except Exception as e:
            print(f"❌ {interval} K线初始化失败: {e}")


# ===============================
# HTTP兜底更新函数（WS断连时手动拉取最新数据）
# 作用：保证行情数据不中断，是WS的备用方案
# ===============================
def http_update():
    print("🔄 执行HTTP兜底更新...")
    url = f"{HTTP_BASE}/fapi/v1/klines"  # K线接口

    for interval in INTERVALS:  # 遍历所有周期
        try:
            # 如果该周期缓存为空，跳过
            if not cache["klines"][interval]:
                continue
            # 获取缓存中最后一条K线的时间
            last_time = cache["klines"][interval][-1]["time"]
            # 请求最新2条K线（用于判断是否有新K线生成）
            params = {"symbol": SYMBOL, "interval": interval, "limit": 2}
            data = requests.get(url, params=params, timeout=5).json()
            new_k = data[-1]  # 最新的一条K线

            # 如果新K线时间不等于最后缓存时间，说明有新K线
            if new_k[0] != last_time:
                with lock:
                    cache["klines"][interval].append({
                        "time": new_k[0],
                        "open": float(new_k[1]),
                        "high": float(new_k[2]),
                        "low": float(new_k[3]),
                        "close": float(new_k[4]),
                        "volume": float(new_k[5])
                    })
                print(f"📈 {interval} K线已更新")
        except Exception as e:
            print(f"⚠️ {interval} K线更新失败: {e}")

    # 价格兜底更新（获取最新成交价）
    try:
        price_url = f"{HTTP_BASE}/fapi/v1/ticker/price"
        r = requests.get(price_url, params={"symbol": SYMBOL}, timeout=3)
        price = float(r.json()["price"])
        update_ws_price(price)  # 更新全局价格缓存
        print(f"💰 价格更新为: {get_price():.4f}")
    except Exception as e:
        print(f"⚠️ 价格更新失败: {e}")

    # 盘口数据兜底更新（获取最新买一/卖一）
    try:
        depth_url = f"{HTTP_BASE}/fapi/v1/depth"
        r = requests.get(depth_url, params={"symbol": SYMBOL, "limit": 1}, timeout=3)
        depth_data = r.json()

        with lock:
            # 提取买一价格（bids第一个元素）
            cache["depth"]["bid"] = float(depth_data["bids"][0][0]) if (
                    depth_data.get("bids") and len(depth_data["bids"]) > 0) else None
            # 提取卖一价格（asks第一个元素）
            cache["depth"]["ask"] = float(depth_data["asks"][0][0]) if (
                    depth_data.get("asks") and len(depth_data["asks"]) > 0) else None

        # 格式化打印盘口数据
        bid_str = f"{cache['depth']['bid']:.4f}" if cache['depth']['bid'] else "N/A"
        ask_str = f"{cache['depth']['ask']:.4f}" if cache['depth']['ask'] else "N/A"
        print(f"📊 盘口更新 - 买一: {bid_str} | 卖一: {ask_str}")
    except Exception as e:
        print(f"⚠️ 盘口更新失败: {str(e)}")


# ===============================
# 技术指标计算函数 - 修复：ATR算法修正
# 输入：K线DataFrame
# 输出：MACD/KDJ/ATR等指标的最新值
# ===============================
def calc_indicators(df):
    # 数据量不足30条时无法计算有效指标
    if len(df) < 30:
        return None

    # 提取核心价格列
    close = df["close"]
    high = df["high"]
    low = df["low"]

    # MACD计算（12/26/9周期）
    ema12 = close.ewm(span=12, adjust=False).mean()  # 12周期EMA
    ema26 = close.ewm(span=26, adjust=False).mean()  # 26周期EMA
    macd = ema12 - ema26                             # MACD线
    signal = macd.ewm(span=9, adjust=False).mean()   # 信号线

    # KDJ计算（9周期）
    low_min = df["low"].rolling(window=9).min()   # 9周期最低价
    high_max = df["high"].rolling(window=9).max() # 9周期最高价
    rsv = (close - low_min) / (high_max - low_min + 1e-9) * 100  # 未成熟随机值（加1e-9防止除0）
    k = rsv.ewm(com=2, adjust=False).mean()       # K值（平滑2期）
    d = k.ewm(com=2, adjust=False).mean()         # D值（平滑2期）
    j = 3 * k - 2 * d                             # J值（3K-2D）

    # ATR计算（平均真实波幅，14周期）- 修复：使用正确的真实波幅计算方式
    tr1 = high - low  # ① 当前K线高低差
    tr2 = (high - close.shift(1)).abs()  # ② 当前高点与前收盘价差的绝对值
    tr3 = (low - close.shift(1)).abs()   # ③ 当前低点与前收盘价差的绝对值
    # 真实波幅TR：取三者中的最大值
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    # 14周期ATR（平均真实波幅）
    atr = tr.rolling(window=14).mean()

    # 返回最新的指标值（取最后一行）
    return {
        "macd": macd.iloc[-1],
        "signal": signal.iloc[-1],
        "k": k.iloc[-1],
        "d": d.iloc[-1],
        "j": j.iloc[-1],
        "atr": atr.iloc[-1],
        "tr": tr.iloc[-1]  # 新增：返回最新真实波幅，便于调试
    }


# ===============================
# 多周期趋势分析函数
# 作用：通过EMA/MACD/收盘价位置判断当前周期的趋势方向
# 输出：强多/偏多/强空/偏空/震荡
# ===============================
def analyze_trend(df):
    # 数据量不足30条无法分析趋势
    if len(df) < 30:
        return "数据不足"

    close = df["close"]
    # 计算EMA12和EMA26（趋势核心指标）
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    # 计算MACD和信号线
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()

    # 取最后3根K线的趋势得分（判断趋势持续性）
    last_n = 3
    scores = []
    for i in range(-last_n, 0):
        score = 0
        # EMA12 > EMA26：多头趋势 +1分，否则 -1分
        if ema12.iloc[i] > ema26.iloc[i]:
            score += 1
        else:
            score -= 1
        # MACD > 信号线：多头动能 +1分，否则 -1分
        if macd.iloc[i] > signal.iloc[i]:
            score += 1
        else:
            score -= 1
        # 收盘价 > EMA12：价格强势 +1分，否则 -1分
        if close.iloc[i] > ema12.iloc[i]:
            score += 1
        else:
            score -= 1
        scores.append(score)

    # 根据得分判断趋势强度
    if all(s >= 2 for s in scores):
        return "🟢 强多"       # 连续3根K线得分≥2：强多头
    elif all(s == 1 for s in scores):
        return "🟡 偏多"       # 连续3根K线得分=1：偏多头
    elif all(s <= -2 for s in scores):
        return "🔴 强空"       # 连续3根K线得分≤-2：强空头
    elif all(s == -1 for s in scores):
        return "🟠 偏空"       # 连续3根K线得分=-1：偏空头
    else:
        return "⚪ 震荡"       # 得分混乱：震荡行情


# ===============================
# WebSocket消息处理函数 - 修复：支持多周期K线更新
# 作用：实时处理WS推送的行情数据，更新缓存
# ===============================
def on_message(ws, message):
    global ws_alive, last_ws_message_ts
    ws_alive = True  # 标记WS连接正常
    last_ws_message_ts = time.time()  # 更新最后消息时间戳

    # 解析WS消息（JSON格式）
    msg = json.loads(message)
    data = msg.get("data", msg)  # 兼容不同格式的WS消息

    # 加锁修改缓存（线程安全）
    with lock:
        # 1. 处理实时成交数据（aggTrade）
        if data.get("e") == "aggTrade":
            price = float(data["p"])  # 成交价格
            update_ws_price(price)    # 更新全局价格缓存

        # 2. 处理标记价格更新（markPriceUpdate）
        elif data.get("e") == "markPriceUpdate":
            current_price = get_price()
            # 如果全局价格为空，用标记价格填充
            if current_price is None:
                price = float(data["p"])
                update_ws_price(price)

        # 3. 处理盘口深度更新（depthUpdate）
        elif data.get("e") == "depthUpdate":
            # 更新买一价格（bids第一个元素）
            cache["depth"]["bid"] = float(data["b"][0][0]) if (data.get("b") and len(data["b"]) > 0) else None
            # 更新卖一价格（asks第一个元素）
            cache["depth"]["ask"] = float(data["a"][0][0]) if (data.get("a") and len(data["a"]) > 0) else None

        # 4. 处理K线更新（kline）
        elif data.get("e") == "kline":
            k = data["k"]
            interval = k["i"]  # ⭐ 识别K线周期（1m/5m等）

            # 构造标准化的K线数据
            new_k = {
                "time": k["t"],        # K线开始时间戳
                "open": float(k["o"]), # 开盘价
                "high": float(k["h"]), # 最高价
                "low": float(k["l"]),  # 最低价
                "close": float(k["c"]),# 收盘价
                "volume": float(k["v"])# 成交量
            }

            # ⭐ 修复：更新对应周期的K线数据
            if interval in cache["klines"]:
                # 检查是否是新的K线（避免重复添加）
                if cache["klines"][interval]:
                    last_k_time = cache["klines"][interval][-1]["time"]
                    if new_k["time"] != last_k_time:
                        cache["klines"][interval].append(new_k)
                        print(f"📈 {interval} K线实时更新 - 时间: {new_k['time']}, 收盘价: {new_k['close']:.4f}")
                else:
                    # 如果缓存为空，直接添加
                    cache["klines"][interval].append(new_k)
                    print(f"📈 {interval} K线初始化补充 - 时间: {new_k['time']}, 收盘价: {new_k['close']:.4f}")


# ===============================
# WebSocket错误/关闭处理函数
# 作用：监控WS连接状态，标记异常
# ===============================
def on_error(ws, error):
    global ws_alive
    ws_alive = False  # 标记WS连接异常
    print(f"❌ WebSocket错误: {error}")


def on_close(ws, *args):
    global ws_alive
    ws_alive = False  # 标记WS连接关闭
    print("⚠️ WebSocket连接关闭")


# ===============================
# 启动WebSocket连接
# 作用：创建WS连接并在独立线程中运行（不阻塞主程序）
# ===============================
def start_ws():
    global last_ws_message_ts
    print("🚀 启动WebSocket连接...")
    last_ws_message_ts = time.time()  # 初始化最后消息时间戳

    # 创建WS应用对象
    ws = websocket.WebSocketApp(
        WS_URL,
        on_message=on_message,  # 消息处理函数
        on_error=on_error,      # 错误处理函数
        on_close=on_close       # 关闭处理函数
    )
    # 启动WS线程（守护线程，主程序退出时自动结束）
    threading.Thread(target=ws.run_forever, daemon=True).start()


# ===============================
# ATR波动过滤器 - 修复：ATR算法修正
# 作用：通过ATR判断市场波动是否适合交易（过滤极端行情）
# 输入：K线DataFrame、周期名称
# 输出：波动状态（过大/过小/正常）
# ===============================
def atr_filter(df, name=""):
    # 数据量不足50条无法计算有效ATR均值
    if len(df) < 50:
        return f"{name} 数据不足"

    # 提取价格列
    high = df["high"]
    low = df["low"]
    close = df["close"]

    # 计算真实波幅TR
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    # 14周期ATR
    atr = tr.rolling(window=14).mean()

    # 当前ATR值和50周期ATR均值（判断波动是否异常）
    current_atr = atr.iloc[-1]
    atr_mean = atr.rolling(50).mean().iloc[-1]

    # 防止除0或空值
    if atr_mean == 0 or np.isnan(atr_mean):
        return f"{name} 数据异常"

    # 判断波动状态
    if current_atr >= atr_mean * 2:
        return f"{name} ❌ 波动过大"    # 波动是均值2倍以上：禁止交易
    elif current_atr <= atr_mean * 0.5:
        return f"{name} ❌ 波动过小"    # 波动是均值0.5倍以下：市场太冷清
    else:
        return f"{name} ✅ 波动正常"    # 波动适中：可交易


# ===============================
# 1分钟周期剃头皮震荡识别 - 优化1：调整区间阈值，解决误触问题
# 作用：识别1分钟周期的震荡区间，判断是否适合剃头皮交易
# 输出：震荡状态、区间高点、区间低点
# ===============================
def detect_scalping_range(df):
    # 数据量不足20条无法识别震荡区间
    if len(df) < 20:
        return "数据不足", None, None

    # 取最近15根K线（短期震荡区间）
    recent = df.tail(15)
    range_high = recent["high"].max()  # 区间高点
    range_low = recent["low"].min()    # 区间低点
    mean_price = recent["close"].mean()# 区间平均价格
    range_pct = (range_high - range_low) / mean_price  # 区间波动百分比

    # 优化1：将阈值从0.002（0.2%）调整为0.0035（0.35%），扩大震荡区间判定范围
    # 波动百分比超过0.35%：非震荡行情（趋势明显）
    if range_pct > 0.0035:
        return "非震荡（波动过大）", None, None

    # 检查是否多次触碰区间高低点（判断区间有效性）
    # 触碰高点次数（价格接近区间高点99.9%）
    touch_high = (recent["high"] > range_high * 0.999).sum()
    # 触碰低点次数（价格接近区间低点100.1%）
    touch_low = (recent["low"] < range_low * 1.001).sum()

    # 至少2次触碰高点和低点：有效震荡区间（可剃头皮）
    if touch_high >= 2 and touch_low >= 2:
        return "🟡 可剃头皮", range_high, range_low
    else:
        return "震荡不足", None, None


# ===============================
# 剃头皮专用ATR过滤 - 修复：ATR算法修正
# 作用：针对剃头皮策略的ATR过滤（阈值更宽松）
# ===============================
def atr_filter_scalping(df):
    # 数据量不足50条无法计算
    if len(df) < 50:
        return "❌ 数据不足"

    # 重新计算ATR（同通用ATR算法）
    high = df["high"]
    low = df["low"]
    close = df["close"]

    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.rolling(window=14).mean()

    current_atr = atr.iloc[-1]
    atr_mean = atr.rolling(50).mean().iloc[-1]

    if atr_mean == 0 or np.isnan(atr_mean):
        return "❌ 数据异常"

    # 剃头皮策略的ATR阈值（更宽松）
    if current_atr >= atr_mean * 1.8:
        return "❌ 波动过大"
    elif current_atr <= atr_mean * 0.6:
        return "❌ 波动过小"
    else:
        return "✅ 可剃头皮"


# ===============================
# 成交量相关判断函数
# 作用：判断成交量是否处于低位/放量（趋势启动信号）
# ===============================
def is_low_volume(df):
    """判断是否为低成交量（缩量整理阶段）"""
    if len(df) < 60:
        return False
    vol = df["volume"]
    last_vol = vol.iloc[-2]  # 上一根K线成交量
    lowest_5 = vol.nsmallest(5)  # 最近5根最低成交量
    avg_low = lowest_5.mean()    # 最低成交量均值
    # 上一根成交量在最低均值的85%-115%之间：低成交量
    return avg_low * 0.85 <= last_vol <= avg_low * 1.15


def is_volume_expand(df):
    """判断是否放量（趋势启动信号）"""
    if len(df) < 2:
        return False
    vol = df["volume"]
    # 最新成交量是上一根的3倍以上：放量
    return vol.iloc[-1] > vol.iloc[-2] * 3


def should_exit_position(df_1m, df_5m, is_long):  #信号反转平仓
    """
    判断是否需要主动平仓
    触发条件：MACD反向 / EMA20破位
    is_long: True=多头持仓，False=空头持仓
    """
    # 1. MACD反向（多头持仓时死叉，空头持仓时金叉）
    if is_long and is_macd_dead_cross(df_5m):
        return True
    if not is_long and is_macd_golden_cross(df_5m):
        return True

    # 2. 跌破/突破EMA20（短线趋势破坏）
    ema20 = df_1m["close"].ewm(span=20).mean().iloc[-1]  # 1分钟EMA20
    price = df_1m["close"].iloc[-1]                      # 最新收盘价

    # 多头持仓：价格跌破EMA20 → 平仓
    if is_long and price < ema20:
        return True
    # 空头持仓：价格突破EMA20 → 平仓
    if not is_long and price > ema20:
        return True

    # 无平仓条件
    return False


# ===============================
# MACD金叉/死叉检测
# 作用：判断MACD是否发生金叉（买入）/死叉（卖出）
# ===============================
def is_macd_golden_cross(df):
    """MACD金叉：MACD线从下向上穿过信号线"""
    if len(df) < 30:
        return False
    close = df["close"]
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    # 上一根K线MACD<信号线，当前K线MACD>信号线 → 金叉
    return macd.iloc[-2] < signal.iloc[-2] and macd.iloc[-1] > signal.iloc[-1]


def is_macd_dead_cross(df):
    """MACD死叉：MACD线从上向下穿过信号线"""
    if len(df) < 30:
        return False
    close = df["close"]
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    # 上一根K线MACD>信号线，当前K线MACD<信号线 → 死叉
    return macd.iloc[-2] > signal.iloc[-2] and macd.iloc[-1] < signal.iloc[-1]


# ===============================
# ATR最终交易判断函数
# 作用：综合双周期ATR状态，判断是否允许交易
# ===============================
def can_trade(atr_1m_status, atr_5m_status):
    if "过大" in atr_1m_status:
        return "❌ 禁止交易（短期波动过大）"
    if "过小" in atr_5m_status:
        return "❌ 禁止交易（市场太平）"
    return "✅ 可以交易"


# ===============================
# 监控主函数 - 核心改造：持仓判断+主动平仓
# 作用：策略核心循环，每5秒执行一次，包含：
# 1. WS兜底检查 2. 数据读取 3. 持仓判断 4. 主动平仓 5. 信号计算 6. 下单执行
# ===============================
def monitor():
    global last_ws_message_ts, last_signal, last_order_time
    # 无限循环执行策略
    while True:
        time.sleep(5)  # 每5秒执行一次（控制策略频率）

        # ===============================
        # 1. WebSocket兜底判断（防止行情中断）
        # ===============================
        current_ts = time.time()
        # 判断WS是否超时（无消息超过阈值）
        ws_timeout = current_ts - last_ws_message_ts > WS_TIMEOUT_SECONDS
        if not ws_alive or ws_timeout:
            if ws_timeout:
                print(f"⚠️ WebSocket无消息超时（{WS_TIMEOUT_SECONDS}秒）→ 切换至HTTP兜底")
            else:
                print("⚠️ WebSocket不可用 → 切换至HTTP兜底")
            # 执行HTTP兜底更新
            http_update()
            last_ws_message_ts = current_ts

        # 价格为空强制更新
        if get_price() is None:
            http_update()

        # ===============================
        # 2. 读取缓存数据（加锁保证线程安全）
        # ===============================
        with lock:
            df_1m = pd.DataFrame(cache["klines"]["1m"])    # 1分钟K线DataFrame
            df_5m = pd.DataFrame(cache["klines"]["5m"])    # 5分钟K线DataFrame
            df_15m = pd.DataFrame(cache["klines"]["15m"])  # 15分钟K线DataFrame
            df_1h = pd.DataFrame(cache["klines"]["1h"])    # 1小时K线DataFrame
            bid_price = cache["depth"]["bid"]              # 买一价格
            ask_price = cache["depth"]["ask"]              # 卖一价格

        # 1分钟K线为空时跳过本次循环
        if df_1m.empty:
            continue

        # ===============================
        # 3. 持仓判断 + 主动平仓 ⭐关键改造
        # ===============================
        try:
            # 查询当前持仓状态（返回"LONG"/"SHORT"/None）
            position_flag = get_position(SYMBOL)

            print(f"\n🔍 持仓查询结果 - {SYMBOL}: {'✅ 有持仓' if position_flag else '❌ 无持仓'}")

            # 如果有持仓，执行主动平仓判断
            if position_flag:
                # 判断持仓方向（多头/LONG，空头/SHORT）
                is_long = position_flag == "LONG"

                # 执行主动平仓条件判断
                if should_exit_position(df_1m, df_5m, is_long):
                    print("⚠️ 触发主动平仓条件")

                    # 执行平仓下单 - ⭐ 核心修改位置 ⭐
                    success = place_order(
                        symbol=SYMBOL,
                        side="SELL" if is_long else "BUY",  # 多单平空，空单平多
                        quantity=DEFAULT_ORDER_QTY,         # 默认下单量
                        order_type="MARKET",                # 市价单平仓（保证成交）
                        reduce_only=True                    # 只减仓（防止开反向仓位）
                    )

                    if success:
                        print("✅ 主动平仓下单成功")
                        # 语音播报平仓结果
                        speak(f"已执行主动平仓，{'多单平仓' if is_long else '空单平仓'}", key="exit", force=True)
                    else:
                        print("❌ 主动平仓下单失败")
                    # 有持仓且处理完平仓后，跳过本次循环（不触发新下单）
                    continue

                # 如果没触发平仓，就不允许开新仓
                entry_signal = "⛔ 持仓中，等待平仓信号"

                # 打印持仓状态信息
                print("\n" + "=" * 30)
                print("📊 实时数据")
                current_price = get_price()
                print(f"价格: {current_price:.4f}" if current_price else "价格: N/A")

                bid_str = f"{bid_price:.4f}" if bid_price is not None else "N/A"
                ask_str = f"{ask_price:.4f}" if ask_price is not None else "N/A"
                print(f"盘口 - 买一: {bid_str} | 卖一: {ask_str}")

                print("\n🎯 策略信号")
                print(f"最终信号: {entry_signal}")
                print(f"下单状态: 未下单")
                print("=" * 30 + "\n")

                continue

        except Exception as e:
            print(f"⚠️ 持仓判断失败：{e}")
            # 持仓查询失败也明确打印
            print(f"🔍 持仓查询结果 - {SYMBOL}: ❌ 查询失败")
            entry_signal = "⛔ 持仓判断异常，暂停交易"
            print("\n" + "=" * 30)
            print(f"最终信号: {entry_signal}")
            print(f"下单状态: 未下单")
            print("=" * 30 + "\n")
            continue

        # ===============================
        # 4. 基础数据计算（无持仓时执行）
        # ===============================
        # 计算1分钟K线的技术指标
        indicators = calc_indicators(df_1m)
        vol_1m = df_1m["volume"]  # 1分钟成交量
        current_price = get_price()  # 最新价格
        if current_price is None:
            print("⚠️ 获取价格失败，跳过")
            continue

        # 打印ATR相关数据（便于调试）
        if indicators:
            print(f"📊 ATR调试数据 - 最新TR: {indicators['tr']:.6f}, 14周期ATR: {indicators['atr']:.6f}")

        # ===============================
        # 5. 策略信号计算
        # ===============================
        # ATR双周期过滤（1分钟+5分钟）
        atr_1m_status = atr_filter(df_1m, "1m")
        atr_5m_status = atr_filter(df_5m, "5m")
        trade_status = can_trade(atr_1m_status, atr_5m_status)

        # 多周期趋势分析（5m/15m/1h）
        trend_5m = analyze_trend(df_5m)
        trend_15m = analyze_trend(df_15m)
        trend_1h = analyze_trend(df_1h)

        # 剃头皮策略核心判断（震荡区间+ATR）
        scalping_range_status, range_high, range_low = detect_scalping_range(df_1m)
        # 新增：打印震荡区间百分比，便于调试
        if range_high and range_low and current_price:
            current_range_pct = (range_high - range_low) / current_price
            print(f"📏 当前震荡区间百分比: {current_range_pct:.4f} (阈值: 0.0035)")

        # 剃头皮ATR过滤
        scalping_atr_status = atr_filter_scalping(df_1m)
        # MACD金叉/死叉
        golden_cross = is_macd_golden_cross(df_5m)
        dead_cross = is_macd_dead_cross(df_5m)

        # 剃头皮信号判断（向上/向下）
        scalp_signal_up = (
                "可剃头皮" in scalping_range_status and  # 震荡区间有效
                "可剃头皮" in scalping_atr_status and    # ATR波动正常
                golden_cross                              # MACD金叉（多头信号）
        )
        scalp_signal_down = (
                "可剃头皮" in scalping_range_status and  # 震荡区间有效
                "可剃头皮" in scalping_atr_status and    # ATR波动正常
                dead_cross                                # MACD死叉（空头信号）
        )

        # 价格边界判断（是否接近震荡区间高低点）
        near_low = False
        near_high = False
        if range_low and range_high:
            near_low = current_price <= range_low * 0.999  # 接近区间低点
            near_high = current_price >= range_high * 1.001 # 接近区间高点

        # 趋势追单判断（缩量+放量+多周期趋势一致+MACD）
        low_vol = is_low_volume(df_1m)          # 低成交量（缩量整理）
        vol_expand = is_volume_expand(df_1m)    # 放量（趋势启动）
        # 多头趋势：15m和1h均为强多/偏多
        trend_long = (
                trend_15m in ["🟢 强多", "🟡 偏多"] and
                trend_1h in ["🟢 强多", "🟡 偏多"]
        )
        # 空头趋势：15m和1h均为强空/偏空
        trend_short = (
                trend_15m in ["🔴 强空", "🟠 偏空"] and
                trend_1h in ["🔴 强空", "🟠 偏空"]
        )

        # 趋势追单信号
        if trend_long and golden_cross and low_vol and vol_expand:
            trend_signal = "🔵 追多"
        elif trend_short and dead_cross and low_vol and vol_expand:
            trend_signal = "🔴 追空"
        else:
            trend_signal = False

        # ===============================
        # 6. 最终交易信号生成（风险过滤）
        # ===============================
        entry_signal = "⛔ 不交易"
        # 向上剃头皮（多头）
        if scalp_signal_up and near_low:
            entry_signal = "🟢 向上剃头皮"
        # 向下剃头皮（空头）
        elif scalp_signal_down and near_high:
            entry_signal = "🟠 向下剃头皮"
        # 趋势追单
        elif trend_signal:
            entry_signal = trend_signal

        # 风险过滤1：价格偏离EMA20超过0.2% → 不交易
        ema20 = df_1m["close"].ewm(span=20, adjust=False).mean().iloc[-1]
        if abs(current_price - ema20) / current_price > 0.002:
            entry_signal = "⛔ 偏离过大，不追"
        # 风险过滤2：ATR风控禁止交易
        if "禁止交易" in trade_status:
            entry_signal = "⛔ ATR风控：禁止交易"
        # 风险过滤3：盘口价差过大（>0.1%）→ 不交易
        if bid_price and ask_price:
            spread_pct = (ask_price - bid_price) / bid_price
            if spread_pct > 0.001:
                entry_signal = "⛔ 盘口价差过大，不交易"

        # 防重复下单过滤（同一信号60秒内不重复下单）
        now = time.time()
        if entry_signal != "⛔ 不交易" and entry_signal == last_signal and now - last_order_time < ORDER_COOLDOWN:
            entry_signal = f"⛔ 信号重复，{int(ORDER_COOLDOWN - (now - last_order_time))}秒后可交易"

        # ===============================
        # 7. 止损止盈价格计算
        # ===============================
        order_status = "未下单"
        stop_loss_value = None
        take_profit_value = None

        # 精准定义多空信号列表
        long_signals = ["🟢 向上剃头皮", "🔵 追多"]
        short_signals = ["🟠 向下剃头皮", "🔴 追空"]

        # 仅当有有效指标且信号为交易信号时计算止损止盈
        if indicators and "atr" in indicators and entry_signal in long_signals + short_signals:
            atr_value = indicators["atr"]
            # 精准判断多空方向
            is_long = entry_signal in long_signals
            is_short = entry_signal in short_signals

            if is_long:
                # 多单止损：当前价格 - ATR*1.5
                stop_loss_value = current_price - atr_value * 1.5
                # 多单止盈：当前价格 + ATR*3
                take_profit_value = current_price + atr_value * 3
            elif is_short:
                # 空单止损：当前价格 + ATR*1.5
                stop_loss_value = current_price + atr_value * 1.5
                # 空单止盈：当前价格 - ATR*3
                take_profit_value = current_price - atr_value * 3

        # ===============================
        # 8. 下单执行
        # ===============================
        if entry_signal in long_signals:  # 多单信号
            success = place_order(
                symbol=SYMBOL,
                side="BUY",  # 做多
                quantity=DEFAULT_ORDER_QTY,
                # 订单类型：剃头皮/SCALPING，趋势/NORMAL
                order_type="SCALPING" if "剃头皮" in entry_signal else "NORMAL",
                # 剃头皮区间（仅剃头皮策略需要）
                scalping_range=(range_low, range_high) if "剃头皮" in entry_signal else None,
                stop_loss=stop_loss_value,  # 止损价格
                take_profit=take_profit_value  # 止盈价格
            )
            if success:
                order_status = "已下多单"
                last_signal = entry_signal  # 记录最后信号
                last_order_time = now       # 记录下单时间
                # 语音播报下单结果
                speak(f"已下多单，策略：{entry_signal}", key="order", force=True)

        elif entry_signal in short_signals:  # 空单信号
            success = place_order(
                symbol=SYMBOL,
                side="SELL",  # 做空
                quantity=DEFAULT_ORDER_QTY,
                order_type="SCALPING" if "剃头皮" in entry_signal else "NORMAL",
                scalping_range=(range_low, range_high) if "剃头皮" in entry_signal else None,
                stop_loss=stop_loss_value,
                take_profit=take_profit_value
            )
            if success:
                order_status = "已下空单"
                last_signal = entry_signal
                last_order_time = now
                speak(f"已下空单，策略：{entry_signal}", key="order", force=True)

        # ===============================
        # 9. 打印策略信息（便于监控和调试）
        # ===============================
        print("\n" + "=" * 30)
        print("📊 实时数据")
        print(f"价格: {current_price:.4f}")
        bid_str = f"{bid_price:.4f}" if bid_price is not None else "N/A"
        ask_str = f"{ask_price:.4f}" if ask_price is not None else "N/A"
        print(f"盘口 - 买一: {bid_str} | 卖一: {ask_str}")
        print(f"成交量均值: {vol_1m.mean():.2f} | 最新成交量: {vol_1m.iloc[-1]:.2f}")
        print(f"最后WS消息时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(last_ws_message_ts))}")
        print(f"持仓状态: {'✅ 有持仓' if position_flag else '❌ 无持仓'}")

        print("\n📈 多周期趋势")
        print(f"5分钟: {trend_5m} | 15分钟: {trend_15m} | 1小时: {trend_1h}")
        # 判断周期趋势是否一致
        trend_sync = "✅ 一致" if trend_5m == trend_15m == trend_1h else "❌ 不一致"
        print(f"周期一致性: {trend_sync}")

        print("\n🛡️ ATR波动过滤")
        print(f"1分钟: {atr_1m_status} | 5分钟: {atr_5m_status}")
        print(f"最终交易状态: {trade_status}")

        print("\n🎯 策略信号")
        print(f"震荡区间状态: {scalping_range_status}")
        print(f"剃头皮ATR状态: {scalping_atr_status}")
        print(f"MACD金叉: {golden_cross} | MACD死叉: {dead_cross}")
        print(f"最终信号: {entry_signal}")
        print(f"下单状态: {order_status}")
        # 打印止损止盈价格（便于验证）
        if stop_loss_value and take_profit_value:
            print(f"止损价格: {stop_loss_value:.4f} | 止盈价格: {take_profit_value:.4f}")
        print("=" * 30 + "\n")


# ===============================
# 主程序入口
# 作用：初始化策略并启动核心监控
# ===============================
def main():
    print("🚀 币安期货交易策略系统启动")

    sync_time()   # ⭐ 必须加：同步服务器时间（解决API签名时间差问题）

    init_klines()  # 初始化历史K线数据
    start_ws()     # 启动WebSocket实时行情
    check_stop_orders(SYMBOL)  # 初始化止盈止损监控
    monitor()      # 启动策略监控主循环


# 程序入口（仅直接运行时执行）
if __name__ == "__main__":
    main()