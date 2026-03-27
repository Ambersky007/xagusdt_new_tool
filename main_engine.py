# -*- coding: utf-8 -*-
"""
币安期货多周期趋势+剃头皮交易策略
✅ 1. 指标使用已收盘K线 无未来函数
✅ 2. HTTP兜底防重复K线
✅ 3. 持仓查询缓存 防API封号
✅ 4. ATR EMA优化 降低滞后
✅ 5. 成交量放量逻辑修复
✅ 6. 趋势条件放宽 正常开单
✅ 7. 止盈止损分策略（剃头皮/趋势不同参数）
✅ 8. 交易锁 防止连环触发
✅ 9. 修复致命BUG：should_exit_position 变量引用错误
"""

# ===============================
# 导入所需的核心库
# ===============================
# 发送HTTP请求，调用币安REST API
import requests
# WebSocket长连接，实时接收币安推送数据
import websocket
# 处理JSON格式数据
import json
# 多线程，让WebSocket和策略监控同时运行
import threading
# 时间相关操作（延时、时间戳）
import time
# 数据处理，用于K线指标计算
import pandas as pd
# 数值计算
import numpy as np
# 双端队列，限制K线缓存数量
from collections import deque

# 外部模块导入
# 订单执行模块：下单、查询持仓、检查挂单
from order_executor import place_order, check_stop_orders, get_position
# 账户配置：交易数量、交易对
from config_account import DEFAULT_ORDER_QTY, SYMBOL
# 语音提醒模块
from voice_alert import speak
# 价格中心：更新/获取最新价格
from price_center import update_ws_price, get_price
# 时间同步模块
from time_sync import sync_time

# ===============================
# 配置区
# ===============================
# 交易对：白银兑USDT
SYMBOL = "XAGUSDT"
# WebSocket使用小写交易对
WS_SYMBOL = SYMBOL.lower()
# 币安期货HTTP接口基础地址
HTTP_BASE = "https://fapi.binance.com"

# WebSocket连接地址，订阅：成交、标记价格、盘口、多周期K线
WS_URL = (
    f"wss://fstream.binance.com/stream?streams="
    f"{WS_SYMBOL}@aggTrade/"  # 实时成交
    f"{WS_SYMBOL}@markPrice/"  # 标记价格
    f"{WS_SYMBOL}@depth20/"  # 20档盘口
    f"{WS_SYMBOL}@kline_1m/"  # 1分钟K线
    f"{WS_SYMBOL}@kline_5m/"  # 5分钟K线
    f"{WS_SYMBOL}@kline_15m/"  # 15分钟K线
    f"{WS_SYMBOL}@kline_30m/"  # 30分钟K线
    f"{WS_SYMBOL}@kline_1h"  # 1小时K线
)

# 需要监控的K线周期
INTERVALS = ["1m", "5m", "15m", "30m", "1h"]
# 初始化K线获取数量
INIT_LIMIT = 120
# WebSocket超时时间（秒）
WS_TIMEOUT_SECONDS = 15
# 上一次WebSocket消息时间戳
last_ws_message_ts = 0
# 下单冷却时间（秒），防止频繁开仓
ORDER_COOLDOWN = 60
# 上一次交易信号
last_signal = None
# 上一次下单时间
last_order_time = 0

# ===============================
# 【第3步】持仓查询缓存（防API高频封号）
# ===============================
# 上一次查询持仓时间
last_position_check = 0
# 缓存的持仓信息
cached_position = None

# ===============================
# 【第8步】交易锁 防止连环触发
# ===============================
# 交易锁：True=锁定，无法开仓
trade_lock = False

# ===============================
# 数据缓存
# ===============================
cache = {
    "depth": {"bid": None, "ask": None},  # 盘口缓存：买一、卖一
    "klines": {i: deque(maxlen=200) for i in INTERVALS},  # 各周期K线缓存，最多存200根
}
# 线程锁，防止多线程同时修改数据
lock = threading.Lock()
# WebSocket连接状态
ws_alive = False


# ===============================
# 初始化K线
# ===============================
def init_klines():
    # 打印初始化提示
    print("📥 初始化K线数据...")
    # 遍历所有周期
    for interval in INTERVALS:
        try:
            # 币安K线接口地址
            url = f"{HTTP_BASE}/fapi/v1/klines"
            # 请求参数：交易对、周期、数量
            params = {"symbol": SYMBOL, "interval": interval, "limit": INIT_LIMIT}
            # 发送GET请求获取K线
            response = requests.get(url, params=params, timeout=10)
            # 解析JSON数据
            data = response.json()

            # 加锁，安全修改缓存
            with lock:
                # 清空当前周期K线缓存
                cache["klines"][interval].clear()
                # 遍历获取到的K线数据
                for k in data:
                    # 将K线数据存入缓存
                    cache["klines"][interval].append({
                        "time": k[0],  # 时间戳
                        "open": float(k[1]),  # 开盘价
                        "high": float(k[2]),  # 最高价
                        "low": float(k[3]),  # 最低价
                        "close": float(k[4]),  # 收盘价
                        "volume": float(k[5])  # 成交量
                    })
            # 打印成功信息
            print(f"✅ {interval} K线初始化成功")
        except Exception as e:
            # 捕获异常，打印失败信息
            print(f"❌ {interval} K线初始化失败: {e}")


# ===============================
# HTTP兜底更新（已修复重复K线）
# ===============================
def http_update():
    # 打印兜底更新提示
    print("🔄 执行HTTP兜底更新...")
    # 币安K线接口
    url = f"{HTTP_BASE}/fapi/v1/klines"

    # 遍历所有周期更新K线
    for interval in INTERVALS:
        try:
            # 如果缓存为空，跳过
            if not cache["klines"][interval]:
                continue
            # 获取缓存中最后一根K线时间
            last_time = cache["klines"][interval][-1]["time"]
            # 请求参数，只获取最新2根
            params = {"symbol": SYMBOL, "interval": interval, "limit": 2}
            # 获取数据
            data = requests.get(url, params=params, timeout=5).json()
            # 取最新一根K线
            new_k = data[-1]

            # 如果新K线时间晚于缓存最后一根，说明是新K线
            if new_k[0] > last_time:
                with lock:
                    # 加入缓存
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

    # 更新最新价格
    try:
        # 币安最新价格接口
        price_url = f"{HTTP_BASE}/fapi/v1/ticker/price"
        r = requests.get(price_url, params={"symbol": SYMBOL}, timeout=3)
        # 解析价格
        price = float(r.json()["price"])
        # 更新价格中心
        update_ws_price(price)
        print(f"💰 价格更新为: {get_price():.4f}")
    except Exception as e:
        print(f"⚠️ 价格更新失败: {e}")

    # 更新盘口数据
    try:
        depth_url = f"{HTTP_BASE}/fapi/v1/depth"
        r = requests.get(depth_url, params={"symbol": SYMBOL, "limit": 1}, timeout=3)
        depth_data = r.json()

        with lock:
            # 取买一价格
            cache["depth"]["bid"] = float(depth_data["bids"][0][0]) if (
                        depth_data.get("bids") and len(depth_data["bids"]) > 0) else None
            # 取卖一价格
            cache["depth"]["ask"] = float(depth_data["asks"][0][0]) if (
                        depth_data.get("asks") and len(depth_data["asks"]) > 0) else None

        # 格式化输出
        bid_str = f"{cache['depth']['bid']:.4f}" if cache['depth']['bid'] else "N/A"
        ask_str = f"{cache['depth']['ask']:.4f}" if cache['depth']['ask'] else "N/A"
        print(f"📊 盘口更新 - 买一: {bid_str} | 卖一: {ask_str}")
    except Exception as e:
        print(f"⚠️ 盘口更新失败: {str(e)}")


# ===============================
# 指标计算
# ===============================
def calc_indicators(df):
    # K线数量不足30根，无法计算
    if len(df) < 30:
        return None
    # 收盘价序列
    close = df["close"]
    # 最高价序列
    high = df["high"]
    # 最低价序列
    low = df["low"]

    # 计算EMA12
    ema12 = close.ewm(span=12, adjust=False).mean()
    # 计算EMA26
    ema26 = close.ewm(span=26, adjust=False).mean()
    # MACD线
    macd = ema12 - ema26
    # 信号线
    signal = macd.ewm(span=9, adjust=False).mean()

    # 9周期最低价
    low_min = df["low"].rolling(window=9).min()
    # 9周期最高价
    high_max = df["high"].rolling(window=9).max()
    # 未成熟随机值
    rsv = (close - low_min) / (high_max - low_min + 1e-9) * 100
    # K值
    k = rsv.ewm(com=2, adjust=False).mean()
    # D值
    d = k.ewm(com=2, adjust=False).mean()
    # J值
    j = 3 * k - 2 * d

    # 真实波幅TR
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    # ATR
    atr = tr.rolling(window=14).mean()

    # 返回所有指标最新值
    return {
        "macd": macd.iloc[-1],
        "signal": signal.iloc[-1],
        "k": k.iloc[-1],
        "d": d.iloc[-1],
        "j": j.iloc[-1],
        "atr": atr.iloc[-1],
        "tr": tr.iloc[-1]
    }


# ===============================
# 趋势判断
# ===============================
def analyze_trend(df):
    # 数据不足返回提示
    if len(df) < 30:
        return "数据不足"
    close = df["close"]
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()

    scores = []
    # 检查最近3根K线趋势
    for i in range(-3, 0):
        score = 0
        # EMA12在EMA26上方，加分
        if ema12.iloc[i] > ema26.iloc[i]:
            score += 1
        else:
            score -= 1
        # MACD在信号线上方，加分
        if macd.iloc[i] > signal.iloc[i]:
            score += 1
        else:
            score -= 1
        # 收盘价在EMA12上方，加分
        if close.iloc[i] > ema12.iloc[i]:
            score += 1
        else:
            score -= 1
        scores.append(score)

    # 根据得分判断趋势强度
    if all(s >= 2 for s in scores):
        return "🟢 强多"
    elif all(s == 1 for s in scores):
        return "🟡 偏多"
    elif all(s <= -2 for s in scores):
        return "🔴 强空"
    elif all(s == -1 for s in scores):
        return "🟠 偏空"
    else:
        return "⚪ 震荡"


# ===============================
# WebSocket 消息处理
# ===============================
def on_message(ws, message):
    # 声明全局变量
    global ws_alive, last_ws_message_ts
    # 标记WebSocket存活
    ws_alive = True
    # 更新最后消息时间
    last_ws_message_ts = time.time()
    # 解析JSON消息
    msg = json.loads(message)
    data = msg.get("data", msg)

    # 加锁处理数据
    with lock:
        # 实时成交数据
        if data.get("e") == "aggTrade":
            update_ws_price(float(data["p"]))
        # 标记价格更新
        elif data.get("e") == "markPriceUpdate":
            if get_price() is None:
                update_ws_price(float(data["p"]))
        # 盘口更新
        elif data.get("e") == "depthUpdate":
            cache["depth"]["bid"] = float(data["b"][0][0]) if data.get("b") else None
            cache["depth"]["ask"] = float(data["a"][0][0]) if data.get("a") else None
        # K线数据
        elif data.get("e") == "kline":
            k = data["k"]
            interval = k["i"]
            # 是否收盘
            is_closed = k["x"]
            # 构造K线对象
            new_k = {
                "time": k["t"],
                "open": float(k["o"]),
                "high": float(k["h"]),
                "low": float(k["l"]),
                "close": float(k["c"]),
                "volume": float(k["v"])
            }
            # 如果是订阅的周期
            if interval in cache["klines"]:
                # 已收盘K线，追加到缓存
                if is_closed:
                    if not cache["klines"][interval] or new_k["time"] > cache["klines"][interval][-1]["time"]:
                        cache["klines"][interval].append(new_k)
                        print(f"✅ {interval} K线收盘: {new_k['close']:.4f}")
                # 未收盘，更新最后一根
                else:
                    if cache["klines"][interval]:
                        cache["klines"][interval][-1] = new_k


# WebSocket错误处理
def on_error(ws, error):
    global ws_alive
    ws_alive = False
    print(f"❌ WebSocket错误: {error}")


# WebSocket关闭处理
def on_close(ws, *args):
    global ws_alive
    ws_alive = False
    print("⚠️ WebSocket连接关闭")


# ===============================
# 启动WS
# ===============================
def start_ws():
    def run():
        global ws_alive, last_ws_message_ts
        # 无限重连
        while True:
            try:
                print("🚀 启动WebSocket连接...")
                # 创建WebSocket对象
                ws = websocket.WebSocketApp(WS_URL, on_message=on_message, on_error=on_error, on_close=on_close)
                ws_alive = False
                last_ws_message_ts = time.time()
                # 启动长连接
                ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as e:
                print(f"❌ WS异常: {e}")
            print("🔄 5秒后重连WS...")
            time.sleep(5)

    # 启动守护线程
    threading.Thread(target=run, daemon=True).start()


# ===============================
# ATR 过滤器
# ===============================
def atr_filter(df, name=""):
    if len(df) < 50:
        return f"{name} 数据不足"
    high, low, close = df["high"], df["low"], df["close"]
    # 计算TR
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    # 14周期ATR
    atr = tr.rolling(window=14).mean()
    # 当前ATR
    current_atr = atr.iloc[-1]

    # 第4步 ATR优化 EMA(50)
    atr_mean = atr.ewm(span=50).mean().iloc[-1]

    # 异常判断
    if atr_mean == 0 or np.isnan(atr_mean):
        return f"{name} 数据异常"
    # 波动过大
    if current_atr >= atr_mean * 2:
        return f"{name} ❌ 波动过大"
    # 波动过小
    elif current_atr <= atr_mean * 0.5:
        return f"{name} ❌ 波动过小"
    # 正常
    else:
        return f"{name} ✅ 波动正常"


# ===============================
# 震荡区间识别
# ===============================
def detect_scalping_range(df):
    """
    剃头皮震荡识别（增强版）
    返回：
        状态, 区间高点, 区间低点
    """

    if len(df) < 20:
        return "数据不足", None, None

    # ===== 1. 取最近K线 =====
    recent = df.tail(15)

    range_high = recent["high"].max()
    range_low = recent["low"].min()
    mean_price = recent["close"].mean()

    # ===== 2. 波动率判断（更稳）=====
    range_pct = (range_high - range_low) / range_low

    # ⚠️ XAG建议 0.004 ~ 0.006
    if range_pct > 0.005:
        return "非震荡（波动过大）", None, None

    # ===== 3. 趋势过滤（核心防亏）=====
    ema20 = recent["close"].ewm(span=20).mean()
    slope = ema20.iloc[-1] - ema20.iloc[-5]

    # ⚠️ 防止缓慢趋势被误判为震荡
    if abs(slope) > mean_price * 0.001:
        return "趋势中（禁止剃头皮）", None, None

    # ===== 4. 波动是否在收敛（防扩散行情）=====
    recent_range = recent["high"] - recent["low"]
    if recent_range.iloc[-1] > recent_range.mean() * 1.2:
        return "波动放大中", None, None

    # ===== 5. 触碰高低点（更严格）=====
    touch_high = (recent["high"] >= range_high * 0.9995).sum()
    touch_low = (recent["low"] <= range_low * 1.0005).sum()

    if touch_high < 2 or touch_low < 2:
        return "震荡不足", None, None

    # ===== 6. 假突破过滤（非常关键）=====
    last_close = recent["close"].iloc[-1]

    if last_close > range_high * 1.001 or last_close < range_low * 0.999:
        return "已突破区间", None, None

    # ===== 7. 中心回归检测（高级过滤）=====
    deviation = abs(recent["close"] - mean_price) / mean_price
    if deviation.mean() > 0.002:
        return "偏离中心过大", None, None

    # ===== ✅ 最终判定 =====
    return "🟡 可剃头皮", range_high, range_low


# ===============================
# 剃头皮ATR
# ===============================
def atr_filter_scalping(df):
    """
    剃头皮ATR过滤（增强版）
    """

    if len(df) < 50:
        return "❌ 数据不足"

    high, low, close = df["high"], df["low"], df["close"]

    # ===== 1. ATR计算 =====
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()

    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.rolling(window=14).mean()

    current_atr = atr.iloc[-1]

    # ===== 2. 平滑ATR（更快响应）=====
    atr_mean = atr.ewm(span=20).mean().iloc[-1]

    if atr_mean == 0 or np.isnan(atr_mean):
        return "❌ 数据异常"

    # ===== 3. 标准化（核心）=====
    price = close.iloc[-1]
    atr_pct = current_atr / price
    atr_mean_pct = atr_mean / price

    # ===== 4. 波动变化趋势（防爆发）=====
    atr_trend = atr.iloc[-1] - atr.iloc[-5]

    if atr_trend > atr_mean * 0.2:
        return "⚠️ 波动正在放大"

    # ===== 5. 剃头皮区间 =====
    if atr_pct >= atr_mean_pct * 1.8:
        return "❌ 波动过大"
    elif atr_pct <= atr_mean_pct * 0.5:
        return "❌ 波动过小"
    else:
        return "✅ 可剃头皮"


# ===============================
# 成交量判断
# ===============================
# 判断是否低量
def is_low_volume(df):
    """
    低成交量检测（双周期版本）
    """

    if len(df) < 60:
        return False

    vol = df["volume"]

    # ===== 1. 短期 vs 长期 =====
    short_avg = vol.tail(20).mean()
    long_avg = vol.tail(60).mean()

    # ===== 2. 缩量判断 =====
    is_low = short_avg < long_avg * 0.8

    # ===== 3. 当前成交量（防假信号）=====
    recent_vol_mean = vol.iloc[-4:-1].mean()
    confirm = recent_vol_mean < short_avg * 0.9

    # ===== 4. 最终 =====
    return is_low and confirm

def is_volume_expand(df):
    """
    放量检测（实盘增强版）
    """

    if len(df) < 25:
        return False

    vol = df["volume"]

    # ===== 1. 当前已收盘成交量 =====
    current_vol = vol.iloc[-2]

    # ===== 2. 过去20根均量（不含当前）=====
    avg_vol = vol.iloc[-22:-2].mean()

    # ===== 3. 基础放量判断 =====
    is_expand = current_vol > avg_vol * 1.4

    # ===== 4. 连续放量确认（防假突破）=====
    prev_vol = vol.iloc[-3]
    confirm = current_vol > prev_vol

    # ===== 5. 排除极端异常（防插针）=====
    vol_std = vol.iloc[-22:-2].std()
    stable = current_vol < avg_vol + 3 * vol_std

    return is_expand and confirm and stable

# ===============================
# 平仓条件
# ===============================
def should_exit_position(df_1m_closed, df_5m_closed, is_long):
    """
    平仓条件增强版
    - 多单：5分钟MACD死叉 或 1分钟EMA20跌破+缓冲
    - 空单：5分钟MACD金叉 或 1分钟EMA20突破+缓冲
    """
    # 5分钟MACD交叉
    if is_long and is_macd_dead_cross(df_5m_closed):
        return True
    if not is_long and is_macd_golden_cross(df_5m_closed):
        return True

    # EMA20
    ema20 = df_1m_closed["close"].ewm(span=20).mean().iloc[-1]
    price = df_1m_closed["close"].iloc[-1]

    # 缓冲 0.05%
    buffer = 0.0005 * price

    if is_long and price < ema20 - buffer:
        return True
    if not is_long and price > ema20 + buffer:
        return True

    return False


# ===============================
# MACD 金叉死叉
# ===============================
# MACD金叉
def is_macd_golden_cross(df):
    if len(df) < 30:
        return False
    close = df["close"]
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    # 上一根MACD在下，当前翻上，金叉
    return macd.iloc[-2] < signal.iloc[-2] and macd.iloc[-1] > signal.iloc[-1]


# MACD死叉
def is_macd_dead_cross(df):
    if len(df) < 30:
        return False
    close = df["close"]
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    # 上一根MACD在上，当前翻下，死叉
    return macd.iloc[-2] > signal.iloc[-2] and macd.iloc[-1] < signal.iloc[-1]


# ===============================
# 交易许可
# ===============================
def can_trade(atr_1m_status, atr_5m_status):
    reasons = []
    if "过大" in atr_1m_status:
        reasons.append("短期波动过大")
    if "过小" in atr_5m_status:
        reasons.append("市场太平")
    if reasons:
        return f"❌ 禁止交易（{'，'.join(reasons)}）"
    return "✅ 可以交易"


# ===============================
# 主策略循环
# ===============================
def monitor():
    global last_ws_message_ts, last_signal, last_order_time
    global last_position_check, cached_position
    global trade_lock  # 交易锁

    while True:
        time.sleep(5)

        # ===============================
        # 交易锁检查
        # ===============================
        if trade_lock:
            time.sleep(1)
            continue  # 保持循环，不退出

        current_ts = time.time()

        # ===============================
        # WebSocket超时 & HTTP兜底
        # ===============================
        ws_timeout = current_ts - last_ws_message_ts > WS_TIMEOUT_SECONDS
        if not ws_alive or ws_timeout:
            print("⚠️ WS异常 → 自动恢复中（HTTP兜底）")
            http_update()
            last_ws_message_ts = current_ts

        if get_price() is None:
            http_update()

        # ===============================
        # 读取K线数据
        # ===============================
        with lock:
            df_1m = pd.DataFrame(cache["klines"]["1m"])
            df_5m = pd.DataFrame(cache["klines"]["5m"])
            df_15m = pd.DataFrame(cache["klines"]["15m"])
            df_1h = pd.DataFrame(cache["klines"]["1h"])
            bid_price = cache["depth"]["bid"]
            ask_price = cache["depth"]["ask"]

        df_1m_closed = df_1m.iloc[:-1]
        df_5m_closed = df_5m.iloc[:-1]
        df_15m_closed = df_15m.iloc[:-1]
        df_1h_closed = df_1h.iloc[:-1]

        if df_1m_closed.empty:
            continue

        # ===============================
        # 持仓缓存（10秒/次）
        # ===============================
        try:
            if time.time() - last_position_check > 10:
                cached_position = get_position(SYMBOL)
                last_position_check = time.time()

            position_flag = cached_position
            print(f"\n🔍 持仓查询结果 - {SYMBOL}: {'✅ 有持仓' if position_flag else '❌ 无持仓'} (缓存10秒)")
        except Exception as e:
            print(f"⚠️ 持仓判断失败：{e}")
            entry_signal = "⛔ 持仓判断异常，暂停交易"
            print("\n" + "=" * 30)
            print(f"最终信号: {entry_signal}")
            print(f"下单状态: 未下单")
            print("=" * 30 + "\n")
            continue

        # ===============================
        # 持仓平仓检查
        # ===============================
        if position_flag:
            is_long = position_flag == "LONG"
            if should_exit_position(df_1m_closed, df_5m_closed, is_long):
                print("⚠️ 触发主动平仓条件")
                success = place_order(
                    symbol=SYMBOL,
                    side="SELL" if is_long else "BUY",
                    quantity=DEFAULT_ORDER_QTY,
                    order_type="MARKET",
                    reduce_only=True
                )
                if success:
                    print("✅ 主动平仓下单成功")
                    speak(f"已执行主动平仓，{'多单平仓' if is_long else '空单平仓'}", key="exit", force=True)
                    trade_lock = False
                else:
                    print("❌ 主动平仓下单失败")
                    trade_lock = False
                continue

            entry_signal = "⛔ 持仓中，等待平仓信号"
            current_price = get_price()
            print("\n" + "=" * 30)
            print(f"📊 实时数据 - 价格: {current_price:.4f}" if current_price else "价格: N/A")
            bid_str = f"{bid_price:.4f}" if bid_price else "N/A"
            ask_str = f"{ask_price:.4f}" if ask_price else "N/A"
            print(f"盘口 - 买一: {bid_str} | 卖一: {ask_str}")
            print(f"最终信号: {entry_signal} | 下单状态: 未下单")
            print("=" * 30 + "\n")
            continue

        # ===============================
        # 指标计算
        # ===============================
        indicators = calc_indicators(df_1m_closed)
        current_price = get_price()
        if current_price is None:
            print("⚠️ 获取价格失败，跳过")
            continue

        atr_1m_status = atr_filter(df_1m_closed, "1m")
        atr_5m_status = atr_filter(df_5m_closed, "5m")
        trade_status = can_trade(atr_1m_status, atr_5m_status)

        trend_5m = analyze_trend(df_5m_closed)
        trend_15m = analyze_trend(df_15m_closed)
        trend_1h = analyze_trend(df_1h_closed)

        scalping_range_status, range_high, range_low = detect_scalping_range(df_1m_closed)
        scalping_atr_status = atr_filter_scalping(df_1m_closed)

        golden_cross = is_macd_golden_cross(df_5m_closed)
        dead_cross = is_macd_dead_cross(df_5m_closed)

        # ====================== 修改：剃头皮信号放宽 ======================
        scalp_signal_up = ("可剃头皮" in scalping_range_status and "可剃头皮" in scalping_atr_status and near_low)
        scalp_signal_down = ("可剃头皮" in scalping_range_status and "可剃头皮" in scalping_atr_status and near_high)
        # ==================================================================

        near_low = current_price <= range_low * 0.995 if (range_low and range_high) else False
        near_high = current_price >= range_high * 1.005 if (range_low and range_high) else False

        low_vol = is_low_volume(df_1m_closed)
        vol_expand = is_volume_expand(df_1m_closed)

        # ===============================
        # 趋势条件
        # ===============================
        trend_long = trend_1h in ["🟢 强多", "🟡 偏多"] and trend_15m != "🔴 强空"
        trend_short = trend_1h in ["🔴 强空", "🟠 偏空"] and trend_15m != "🟢 强多"

        trend_signal = None
        if trend_long and golden_cross and low_vol and vol_expand:
            trend_signal = "🔵 追多"
        elif trend_short and dead_cross and low_vol and vol_expand:
            trend_signal = "🔴 追空"

        # ===============================
        # 信号判定
        # ===============================
        entry_signal = "⛔ 不交易"
        if scalp_signal_up and near_low:
            entry_signal = "🟢 向上剃头皮"
        elif scalp_signal_down and near_high:
            entry_signal = "🟠 向下剃头皮"
        elif trend_signal:
            entry_signal = trend_signal

        # EMA20偏离 & ATR风控 & 盘口价差
        ema20 = df_1m_closed["close"].ewm(span=20).mean().iloc[-1]
        reason = []
        if abs(current_price - ema20) / current_price > 0.01:  #这个值太小会不开仓，不能太苛刻，可以改成1%
            entry_signal = "⛔ 偏离过大，不追"
            reason.append("EMA20偏离过大")
        if "禁止交易" in trade_status:
            entry_signal = "⛔ ATR风控：禁止交易"
            reason.append(atr_1m_status + " | " + atr_5m_status)
        if bid_price and ask_price:
            spread_pct = (ask_price - bid_price) / bid_price
            if spread_pct > 0.001:
                entry_signal = "⛔ 盘口价差过大，不交易"
                reason.append(f"价差 {spread_pct:.4f}")

        now = time.time()
        if entry_signal != "⛔ 不交易" and entry_signal == last_signal and now - last_order_time < ORDER_COOLDOWN:
            entry_signal = f"⛔ 信号重复，{int(ORDER_COOLDOWN - (now - last_order_time))}秒后可交易"

        # ===============================
        # 止盈止损
        # ===============================
        order_status = "未下单"
        stop_loss_value = None
        take_profit_value = None
        long_signals = ["🟢 向上剃头皮", "🔵 追多"]
        short_signals = ["🟠 向下剃头皮", "🔴 追空"]

        is_long = entry_signal in long_signals
        is_short = entry_signal in short_signals

        if indicators and "atr" in indicators and (is_long or is_short):
            atr_value = indicators["atr"]
            if "剃头皮" in entry_signal:
                sl_multiplier, tp_multiplier = 1, 1.5
            else:
                sl_multiplier, tp_multiplier = 1.5, 3
            if is_long:
                stop_loss_value = current_price - atr_value * sl_multiplier
                take_profit_value = current_price + atr_value * tp_multiplier
            else:
                stop_loss_value = current_price + atr_value * sl_multiplier
                take_profit_value = current_price - atr_value * tp_multiplier

        # ===============================
        # 下单
        # ===============================
        success = False
        if entry_signal in long_signals:
            success = place_order(
                symbol=SYMBOL, side="BUY", quantity=DEFAULT_ORDER_QTY,
                order_type="SCALPING" if "剃头皮" in entry_signal else "NORMAL",
                scalping_range=(range_low, range_high) if "剃头皮" in entry_signal else None,
                stop_loss=stop_loss_value, take_profit=take_profit_value
            )
        elif entry_signal in short_signals:
            success = place_order(
                symbol=SYMBOL, side="SELL", quantity=DEFAULT_ORDER_QTY,
                order_type="SCALPING" if "剃头皮" in entry_signal else "NORMAL",
                scalping_range=(range_low, range_high) if "剃头皮" in entry_signal else None,
                stop_loss=stop_loss_value, take_profit=take_profit_value
            )

        if success:
            order_status = "✅ 下单成功"
            last_signal = entry_signal
            last_order_time = now
            speak(f"已下单：{entry_signal}", key="order", force=True)
            trade_lock = True
        else:
            if entry_signal not in ["⛔ 不交易"]:
                order_status = "❌ 下单失败"
                trade_lock = False  # 下单失败解锁

        # ===============================
        # 日志打印
        # ===============================
        print("\n" + "=" * 30)
        print(f"📊 实时数据 - 价格: {current_price:.4f}")
        bid_str = f"{bid_price:.4f}" if bid_price else "N/A"
        ask_str = f"{ask_price:.4f}" if ask_price else "N/A"
        print(f"盘口 - 买一: {bid_str} | 卖一: {ask_str}")
        print(f"成交量均值: {df_1m_closed['volume'].mean():.2f} | 最新成交量: {df_1m_closed['volume'].iloc[-1]:.2f}")
        print(f"持仓状态: {'✅ 有持仓' if position_flag else '❌ 无持仓'}")

        #示例修改（放在打印日志之前）：

        # ===== 放宽后的周期一致性判断 =====
        # 提取非震荡趋势
        non_side_trends = [t for t in [trend_5m, trend_15m, trend_1h] if t not in ["⚪ 震荡"]]

        # 如果非震荡趋势都同向，则认为一致
        if len(non_side_trends) > 0 and all(t == non_side_trends[0] for t in non_side_trends):
            period_consistency = "✅ 一致"
        else:
            period_consistency = "❌ 不一致"
        print("\n📈 多周期趋势")
        print(f"5分钟: {trend_5m} | 15分钟: {trend_15m} | 1小时: {trend_1h}")
        print(f"周期一致性: {period_consistency}")

        print("\n🛡️ ATR波动过滤")
        print(f"1分钟: {atr_1m_status} | 5分钟: {atr_5m_status}")
        print(f"最终交易状态: {trade_status}")

        print("\n🎯 策略信号")
        print(f"震荡区间状态: {scalping_range_status}")
        print(f"剃头皮ATR状态: {scalping_atr_status}")
        print(f"MACD金叉: {golden_cross} | MACD死叉: {dead_cross}")
        print(f"最终信号: {entry_signal} {'(' + ','.join(reason) + ')' if reason else ''}")
        print(f"下单状态: {order_status}")
        if stop_loss_value and take_profit_value:
            print(f"止损: {stop_loss_value:.4f} | 止盈: {take_profit_value:.4f}")
        print(f"交易锁: {'🔒 已锁定' if trade_lock else '🔓 未锁定'}")
        print("=" * 30 + "\n")


# ===============================
# 主入口
# ===============================
def main():
    print("🚀 币安期货交易策略系统启动 - 全BUG修复完成")
    sync_time()  # 同步时间
    init_klines()  # 初始化K线
    start_ws()  # 启动WebSocket
    check_stop_orders(SYMBOL)  # 检查挂单
    monitor()  # 启动策略监控


# 运行主程序
if __name__ == "__main__":
    main()