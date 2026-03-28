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
✅ 10. 修复周期一致性判断错误（2026-03-27 最新修复）
✅ 11. 修复ATR波动过滤：增加15m+1h 完整4周期风控（2026-03-27 最新修复）
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
from dxy_calculator import update_dxy, filtered_trend  # 你写的DXY模块
import dxy_calculator

from order_executor import place_order, check_stop_orders, get_position, wait_order_filled

# ===============================
# 配置区
# ===============================
# 交易对：白银兑USDT
SYMBOL = "XAGUSDT"
# WebSocket使用小写交易对（币安要求）
WS_SYMBOL = SYMBOL.lower()
# 币安期货HTTP接口基础地址
HTTP_BASE = "https://fapi.binance.com"

# WebSocket连接地址，订阅：成交、标记价格、盘口、多周期K线
WS_URL = (
    f"wss://fstream.binance.com/stream?streams="
    f"{WS_SYMBOL}@aggTrade/"
    f"{WS_SYMBOL}@markPrice/"
    f"{WS_SYMBOL}@depth/"        # ✅ 只改这一行！用 depth 实时增量流
    f"{WS_SYMBOL}@kline_1m/"
    f"{WS_SYMBOL}@kline_5m/"
    f"{WS_SYMBOL}@kline_15m/"
    f"{WS_SYMBOL}@kline_30m/"
    f"{WS_SYMBOL}@kline_1h"
)

# 需要监控的K线周期列表
INTERVALS = ["1m", "5m", "15m", "30m", "1h"]
# 初始化K线时，从API获取的K线数量
INIT_LIMIT = 120
# WebSocket超时时间（秒），超过则触发HTTP兜底
WS_TIMEOUT_SECONDS = 15
# 上一次WebSocket消息时间戳，用于判断连接是否存活
last_ws_message_ts = 0
# 下单冷却时间（秒），防止频繁开仓触发风控
ORDER_COOLDOWN = 60
# 上一次交易信号（多/空/不交易）
last_signal = None
# 上一次下单时间戳
last_order_time = 0
# HTTP 兜底冷却（防封号必修）
last_http_update = 0
HTTP_COOLDOWN = 20

# 币安价格精度（XAGUSDT = 2位小数）
PRICE_PRECISION = 2

# ===============================
# 【第3步】持仓查询缓存（防API高频封号）
# ===============================
# 上一次查询持仓的时间戳
last_position_check = 0
# 缓存的持仓信息，避免频繁调用API
cached_position = None

# ===============================
# 【第8步】交易锁 防止连环触发
# ===============================
# 交易锁：True=锁定，无法开仓，防止连续下单
trade_lock = False
TRADE_LOCK_TIMEOUT = 120  # 最大锁定时间（秒）
trade_lock_time = 0  # 锁定开始时间

# ===============================
# 数据缓存
# ===============================
# 全局数据缓存字典
cache = {
    "depth": {"bid": None, "ask": None},  # 盘口缓存：买一价格、卖一价格
    "klines": {i: deque(maxlen=200) for i in INTERVALS},  # 各周期K线缓存，最多存200根
}
# 线程锁，防止多线程同时修改数据造成冲突
lock = threading.Lock()
# WebSocket连接状态：True=连接正常
ws_alive = False


# ===============================
# 初始化K线
# ===============================
def init_klines():
    """
    程序启动时，通过HTTP API一次性拉取所有周期的历史K线
    填充到缓存中，避免WebSocket刚连接时无数据
    """
    # 打印初始化提示
    print("📥 初始化K线数据...")
    # 遍历所有需要监控的K线周期
    for interval in INTERVALS:
        try:
            # 币安期货K线API接口地址
            url = f"{HTTP_BASE}/fapi/v1/klines"
            # 请求参数：交易对、K线周期、获取数量
            params = {"symbol": SYMBOL, "interval": interval, "limit": INIT_LIMIT}
            # 发送GET请求获取K线数据，超时10秒
            response = requests.get(url, params=params, timeout=10)
            # 将响应结果解析为JSON格式
            data = response.json()

            # 加锁，保证多线程下安全修改缓存数据
            with lock:
                # 清空当前周期的K线缓存
                cache["klines"][interval].clear()
                # 遍历获取到的每一根K线数据
                for k in data:
                    # 将K线数据格式化后存入缓存
                    cache["klines"][interval].append({
                        "time": k[0],  # K线开始时间戳
                        "open": float(k[1]),  # 开盘价
                        "high": float(k[2]),  # 最高价
                        "low": float(k[3]),  # 最低价
                        "close": float(k[4]),  # 收盘价
                        "volume": float(k[5])  # 成交量
                    })
            # 打印当前周期K线初始化成功
            print(f"✅ {interval} K线初始化成功")
        except Exception as e:
            # 捕获异常，打印失败信息
            print(f"❌ {interval} K线初始化失败: {e}")


# ===============================
# HTTP兜底更新（已修复重复K线）
# ===============================
def http_update():
    """
    WebSocket断开/超时/无数据时，通过HTTP API主动拉取最新数据
    保证策略不会因WS中断而停止运行
    """
    global last_http_update
    # HTTP 兜底冷却（防封号必修）
    if time.time() - last_http_update < HTTP_COOLDOWN:
        return
    last_http_update = time.time()

    # 打印兜底更新提示
    print("🔄 执行HTTP兜底更新...")
    # 币安K线API地址
    url = f"{HTTP_BASE}/fapi/v1/klines"

    # 遍历所有周期，更新K线数据
    for interval in INTERVALS:
        try:
            # 如果当前周期缓存为空，跳过更新
            if not cache["klines"][interval]:
                continue
            # 获取缓存中最后一根K线的时间戳
            last_time = cache["klines"][interval][-1]["time"]
            # 请求参数，只获取最新2根K线
            params = {"symbol": SYMBOL, "interval": interval, "limit": 2}
            # 获取最新K线数据
            data = requests.get(url, params=params, timeout=5).json()

            # 修复2：只取已收盘K线 data[-2]
            if len(data) >= 2:
                new_k = data[-2]

                # 如果新K线时间晚于缓存最后一根，说明是新K线，避免重复添加
                if new_k[0] > last_time:
                    with lock:
                        # 将新K线加入缓存
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

    # 更新最新市场价格
    try:
        # 币安最新价格API
        price_url = f"{HTTP_BASE}/fapi/v1/ticker/price"
        r = requests.get(price_url, params={"symbol": SYMBOL}, timeout=3)
        # 解析并转换为浮点数
        price = float(r.json()["price"])
        # 更新全局价格中心
        update_ws_price(price)
        print(f"💰 价格更新为: {get_price():.4f}")
    except Exception as e:
        print(f"⚠️ 价格更新失败: {e}")

    # 更新盘口数据（买一、卖一）
    try:
        depth_url = f"{HTTP_BASE}/fapi/v1/depth"
        r = requests.get(depth_url, params={"symbol": SYMBOL, "limit": 1}, timeout=3)
        depth_data = r.json()

        with lock:
            # 获取买一价格，如果有数据则转换为浮点数
            cache["depth"]["bid"] = float(depth_data["bids"][0][0]) if (
                    depth_data.get("bids") and len(depth_data["bids"]) > 0) else None
            # 获取卖一价格
            cache["depth"]["ask"] = float(depth_data["asks"][0][0]) if (
                    depth_data.get("asks") and len(depth_data["asks"]) > 0) else None

        # 格式化输出盘口价格，无数据则显示N/A
        bid_str = f"{cache['depth']['bid']:.4f}" if cache['depth']['bid'] else "N/A"
        ask_str = f"{cache['depth']['ask']:.4f}" if cache['depth']['ask'] else "N/A"
        print(f"📊 盘口更新 - 买一: {bid_str} | 卖一: {ask_str}")
    except Exception as e:
        print(f"⚠️ 盘口更新失败: {str(e)}")


# ===============================
# 指标计算
# ===============================
def calc_indicators(df):
    """
    输入K线DataFrame，计算技术指标：MACD、KDJ、ATR
    返回最新一根K线的所有指标值
    """
    # K线数量不足30根，无法计算有效指标，返回空
    if len(df) < 30:
        return None
    # 提取收盘价序列
    close = df["close"]
    # 提取最高价序列
    high = df["high"]
    # 提取最低价序列
    low = df["low"]

    # 计算12周期指数移动平均线
    ema12 = close.ewm(span=12, adjust=False).mean()
    # 计算26周期指数移动平均线
    ema26 = close.ewm(span=26, adjust=False).mean()
    # MACD线 = EMA12 - EMA26
    macd = ema12 - ema26
    # 信号线 = MACD的9周期EMA
    signal = macd.ewm(span=9, adjust=False).mean()

    # 9周期最低价
    low_min = df["low"].rolling(window=9).min()
    # 9周期最高价
    high_max = df["high"].rolling(window=9).max()
    # 未成熟随机值RSV
    rsv = (close - low_min) / (high_max - low_min + 1e-9) * 100
    # K值 = RSV的平滑移动平均
    k = rsv.ewm(com=2, adjust=False).mean()
    # D值 = K值的平滑移动平均
    d = k.ewm(com=2, adjust=False).mean()
    # J值 = 3*K - 2*D
    j = 3 * k - 2 * d

    # 真实波幅TR的三个计算方式
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    # TR取三者最大值
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    # ATR = TR的14周期简单平均
    atr = tr.rolling(window=14).mean()

    # 返回所有指标的最新值
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
    """
    根据EMA和MACD综合判断趋势：强多/偏多/强空/偏空/震荡
    用于多周期共振判断
    """
    # 数据不足返回提示
    if len(df) < 30:
        return "数据不足"
    close = df["close"]
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()

    scores = []
    # 检查最近3根K线趋势，进行打分
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
    score_sum = sum(scores)

    if score_sum >= 4:
        return "🟢 强多"
    elif score_sum >= 2:
        return "🟡 偏多"
    elif score_sum <= -4:
        return "🔴 强空"
    elif score_sum <= -2:
        return "🟠 偏空"
    else:
        return "⚪ 震荡"


# ===============================
# WebSocket 消息处理
# ===============================
def on_message(ws, message):
    global ws_alive, last_ws_message_ts
    ws_alive = True
    last_ws_message_ts = time.time()
    msg = json.loads(message)

    # 组合流必须拆分 stream + data
    stream = msg.get("stream", "")
    data = msg.get("data", msg)

    with lock:
        # 成交数据
        if data.get("e") == "aggTrade":
            update_ws_price(float(data["p"]))

        # 标记价格
        elif data.get("e") == "markPriceUpdate":
            if get_price() is None:
                update_ws_price(float(data["p"]))

        # ==========================================
        # ✅ 正确：depthUpdate 实时盘口（你现在的逻辑可用了）
        # ==========================================
        elif data.get("e") == "depthUpdate":
            bids = data.get("b", [])
            asks = data.get("a", [])

            # 安全取值，绝不崩溃
            if len(bids) > 0:
                cache["depth"]["bid"] = float(bids[0][0])
            if len(asks) > 0:
                cache["depth"]["ask"] = float(asks[0][0])

        # K线
        elif data.get("e") == "kline":
            k = data["k"]
            interval = k["i"]
            is_closed = k["x"]
            new_k = {
                "time": k["t"],
                "open": float(k["o"]),
                "high": float(k["h"]),
                "low": float(k["l"]),
                "close": float(k["c"]),
                "volume": float(k["v"])
            }
            if interval in cache["klines"]:
                if is_closed:
                    if not cache["klines"][interval] or new_k["time"] > cache["klines"][interval][-1]["time"]:
                        cache["klines"][interval].append(new_k)
                else:
                    if cache["klines"][interval]:
                        cache["klines"][interval][-1] = new_k


# WebSocket错误处理回调
def on_error(ws, error):
    global ws_alive
    ws_alive = False
    print(f"❌ WebSocket错误: {error}")


# WebSocket关闭回调
def on_close(ws, *args):
    global ws_alive
    ws_alive = False
    print("⚠️ WebSocket连接关闭")


# ===============================
# 启动WS（修复5：指数退避重连）
# ===============================
def start_ws():
    def run():
        global ws_alive, last_ws_message_ts
        reconnect_delay = 5
        while True:
            try:
                print("🚀 启动WebSocket连接...")

                # ===============================
                # ✅ 必修修复：重连前清空K线缓存
                # ===============================
                with lock:
                    for interval in INTERVALS:
                        cache["klines"][interval].clear()
                init_klines()  # 重新拉取正确K线
                time.sleep(1)

                ws = websocket.WebSocketApp(WS_URL, on_message=on_message, on_error=on_error, on_close=on_close)
                ws_alive = False
                last_ws_message_ts = time.time()
                ws.run_forever(ping_interval=20, ping_timeout=10)
                reconnect_delay = 5
            except Exception as e:
                print(f"❌ WS异常: {e}")
            print(f"🔄 {reconnect_delay}秒后重连WS...")
            time.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 1.5, 60)

    threading.Thread(target=run, daemon=True).start()


# ===============================
# ATR 过滤器（修复6：空值防护）
# ===============================
def atr_filter(df, name=""):
    if len(df) < 50:
        return f"{name} 数据不足"

    high, low, close = df["high"], df["low"], df["close"]
    price = close.iloc[-1]

    # ✅ 加固：防止价格为 0 除零崩溃
    if price < 1e-6:
        return "❌ 价格异常"
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()

    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.rolling(window=14).mean()

    if atr.empty or pd.isna(atr.iloc[-1]):
        return "❌ 指标异常"

    current_atr = atr.iloc[-1]
    atr_mean = atr.ewm(span=50).mean().iloc[-1]

    if atr_mean == 0 or np.isnan(atr_mean):
        return f"{name} 数据异常"

    price = close.iloc[-1]
    atr_pct = current_atr / price

    if atr_pct > 0.01:
        return f"{name} 🚀 高波动"
    elif atr_pct < 0.002:
        return f"{name} 💤 低波动"
    else:
        return f"{name} ✅ 正常"


# ===============================
# 震荡区间识别
# ===============================
def detect_scalping_range(df):
    if len(df) < 50:
        return "数据不足", None, None

    recent = df.tail(50)

    high = recent["high"].max()
    low = recent["low"].min()
    mid = (high + low) / 2

    # 上下分布
    above = (recent["close"] > mid).sum()
    below = (recent["close"] < mid).sum()

    balance = min(above, below) / max(above, below)

    range_pct = (high - low) / mid

    if balance < 0.6:
        return "单边走势", None, None

    if range_pct > 0.01:
        return "波动过大", None, None

    if range_pct < 0.002:
        return "波动过小", None, None

    return "🟡 可剃头皮", high, low


# ===============================
# 剃头皮ATR（修复6：空值防护）
# ===============================
def atr_filter_scalping(df):
    """
    剃头皮专用ATR过滤（增强版）
    波动必须在合理区间才允许剃头皮
    """

    if len(df) < 50:
        return "❌ 数据不足"

    high, low, close = df["high"], df["low"], df["close"]
    price = close.iloc[-1]

    # ✅ 加固：防止价格为 0 除零崩溃
    if price < 1e-6:
        return "❌ 价格异常"
    # ===== 1. ATR计算 =====
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()

    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.rolling(window=14).mean()

    if atr.empty or pd.isna(atr.iloc[-1]):
        return "❌ 指标异常"

    current_atr = atr.iloc[-1]

    # ===== 2. 平滑ATR =====
    atr_mean = atr.ewm(span=20).mean().iloc[-1]

    if atr_mean == 0 or np.isnan(atr_mean):
        return "❌ 数据异常"

    # ===== 3. 价格标准化 =====
    price = close.iloc[-1]
    atr_pct = current_atr / price
    atr_mean_pct = atr_mean / price

    # ===== 4. 波动变化趋势（防止突然爆发） =====
    atr_trend = atr.iloc[-1] - atr.iloc[-5]

    if atr_trend > atr_mean * 0.2:
        return "⚠️ 波动正在放大"

    # ===== 5. 剃头皮波动区间 =====
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
    用于确认缩量回调
    """

    if len(df) < 60:
        return False

    vol = df["volume"]

    # ===== 1. 短期均量 vs 长期均量 =====
    short_avg = vol.tail(20).mean()
    long_avg = vol.tail(60).mean()

    # ===== 2. 缩量判断 =====
    is_low = short_avg < long_avg * 0.8

    # ===== 3. 当前成交量确认 =====
    recent_vol_mean = vol.iloc[-4:-1].mean()
    confirm = recent_vol_mean < short_avg * 0.9

    # ===== 4. 最终低量结果 =====
    return is_low and confirm


def is_volume_expand(df):
    """
    放量检测（实盘增强版）
    放量确认突破信号
    """
    # ✅ 必修修复：先判断长度，防止索引越界崩溃
    if len(df) < 25:
        return False

    price_up = df["close"].iloc[-2] > df["close"].iloc[-3]
    price_down = df["close"].iloc[-2] < df["close"].iloc[-3]
    if not (price_up or price_down):
        return False

    vol = df["volume"]
    current_vol = vol.iloc[-2]
    avg_vol = vol.iloc[-22:-2].mean()
    is_expand = current_vol > avg_vol * 1.2  #这个值关乎追单的阈值，越大越不容易追
    prev_vol = vol.iloc[-3]
    confirm = current_vol > prev_vol
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
    # ✅ 修复：防止K线数量不足导致程序崩溃
    if len(df_1m_closed) < 20 or len(df_5m_closed) < 30:
        return False

    # 5分钟MACD交叉平仓
    if is_long and is_macd_dead_cross(df_5m_closed):
        return True
    if not is_long and is_macd_golden_cross(df_5m_closed):
        return True

    # 1分钟EMA20平仓
    ema20 = df_1m_closed["close"].ewm(span=20).mean().iloc[-1]
    price = df_1m_closed["close"].iloc[-1]

    # 价格缓冲 0.05%，防止毛刺触发
    buffer = 0.0005 * price

    # 多单：价格跌破EMA20 - 缓冲
    if is_long and price < ema20 - buffer:
        return True
    # 空单：价格涨破EMA20 + 缓冲
    if not is_long and price > ema20 + buffer:
        return True

    # 不满足平仓条件
    return False


# ===============================
# 默认止盈止损（ATR异常兜底，纯填充、不冲突订单模块）
# ===============================
def default_sl_tp(price):
    """
    指标/ATR报错时兜底赋值，保证SL/TP永远是数字、不卡下单
    不生效于实际平仓，只填参数防空值拒单
    """
    sl = price * 0.992
    tp = price * 1.008
    return round(sl, PRICE_PRECISION), round(tp, PRICE_PRECISION)

# ===============================
# 主入口
# ===============================
def main():
    ...

# ===============================
# MACD 金叉死叉
# ===============================
# MACD金叉判断
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


# MACD死叉判断
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
# 交易许可 【已修复：4周期ATR】
# ===============================
def can_trade(atr_1m_status, atr_5m_status, atr_15m_status, atr_1h_status):
    """
    ATR市场分类器（实战版）

    ❗ 不再用于“禁止交易”
    ✅ 只用于判断市场类型，从而决定策略方向

    返回：
        🚀 趋势市场（优先做趋势）
        💤 震荡市场（优先剃头皮）
        ⚖️ 混合市场（谨慎交易）
    """

    statuses = [
        atr_1m_status,
        atr_5m_status,
        atr_15m_status,
        atr_1h_status
    ]

    # ===== 统计各类型数量 =====
    high_vol_count = sum(1 for s in statuses if "高波动" in s)
    low_vol_count = sum(1 for s in statuses if "低波动" in s)
    normal_count = sum(1 for s in statuses if "正常" in s)

    # ===== 市场状态判断 =====

    # 趋势市场：至少2个周期高波动
    if high_vol_count >= 2:
        return "🚀 趋势市场（优先做趋势）"

    # 震荡市场：至少2个周期低波动
    elif low_vol_count >= 2:
        return "💤 震荡市场（优先剃头皮）"

    # 混合市场：无明显结构
    else:
        return "⚖️ 混合市场（谨慎交易）"


# ===============================
# 主策略循环
# ===============================
def get_entry_signal(current_price, scalping_range_status, scalping_atr_status, near_low, near_high, trend_signal):
    """
    综合所有条件，生成最终开仓信号
    优先剃头皮，其次趋势交易
    """
    # 剃头皮做多条件：震荡+ATR合格+价格靠近区间低位
    scalp_up = "可剃头皮" in scalping_range_status and "可剃头皮" in scalping_atr_status and near_low
    # 剃头皮做空条件：震荡+ATR合格+价格靠近区间高位
    scalp_down = "可剃头皮" in scalping_range_status and "可剃头皮" in scalping_atr_status and near_high

    if scalp_up:
        return "🟢 向上剃头皮"
    elif scalp_down:
        return "🟠 向下剃头皮"
    elif trend_signal:
        return trend_signal
    else:
        return "⛔ 不交易"


def calculate_sl_tp(current_price, atr_value, entry_signal):
    """
    根据信号类型（剃头皮/趋势）计算不同的止盈止损
    剃头皮：小止盈小止损
    趋势：大止盈大止损
    """
    if "剃头皮" in entry_signal:
        sl_multiplier, tp_multiplier = 1, 1.5
    else:
        sl_multiplier, tp_multiplier = 1.5, 3

    if entry_signal in ["🟢 向上剃头皮", "🔵 追多"]:
        stop_loss = current_price - atr_value * sl_multiplier
        take_profit = current_price + atr_value * tp_multiplier
    elif entry_signal in ["🟠 向下剃头皮", "🔴 追空"]:
        stop_loss = current_price + atr_value * sl_multiplier
        take_profit = current_price - atr_value * tp_multiplier
    else:
        stop_loss = take_profit = None

    # ✅ 币安强制价格精度修复（必修）
    if stop_loss is not None:
        stop_loss = round(stop_loss, PRICE_PRECISION)
    if take_profit is not None:
        take_profit = round(take_profit, PRICE_PRECISION)

    return stop_loss, take_profit


def print_status(current_price, bid_price, ask_price, df_1m_closed, position_flag,
                 trend_5m, trend_15m, trend_1h,
                 atr_1m_status, atr_5m_status, atr_15m_status, atr_1h_status,
                 trade_status, scalping_range_status, scalping_atr_status,
                 near_low, near_high, golden_cross, dead_cross, entry_signal,
                 order_status, stop_loss_value, take_profit_value, trade_lock, reason,
                 dxy_trend, dxy_signal):
    """
    控制台打印实时策略状态，方便监控
    """
    print("\n" + "=" * 30)
    print(f"📊 实时数据 - 价格: {current_price:.4f}")
    bid_str = f"{bid_price:.4f}" if bid_price else "N/A"
    ask_str = f"{ask_price:.4f}" if ask_price else "N/A"
    print(f"盘口 - 买一: {bid_str} | 卖一: {ask_str}")
    print(f"成交量均值: {df_1m_closed['volume'].mean():.2f} | 最新成交量: {df_1m_closed['volume'].iloc[-1]:.2f}")
    print(f"持仓状态: {'✅ 有持仓' if position_flag else '❌ 无持仓'}")

    # 周期一致性判断
    trend_list = [trend_5m, trend_15m, trend_1h]
    valid_trends = [t for t in trend_list if t not in ["⚪ 震荡", "数据不足"]]
    if len(valid_trends) <= 1:
        period_consistency = "❌ 不一致"
    else:
        first = valid_trends[0]
        all_same = all(t == first for t in valid_trends)
        period_consistency = "✅ 一致" if all_same else "❌ 不一致"

    print("\n📈 多周期趋势")
    print(f"5分钟: {trend_5m} | 15分钟: {trend_15m} | 1小时: {trend_1h}")
    print(f"周期一致性: {period_consistency}")

    print("\n🛡️ ATR波动过滤")
    print(f"1m: {atr_1m_status} | 5m: {atr_5m_status} | 15m: {atr_15m_status} | 1h: {atr_1h_status}")
    print(f"最终交易状态: {trade_status}")

    print("\n🎯 策略信号")
    print(f"震荡区间状态: {scalping_range_status}")
    print(f"剃头皮ATR状态: {scalping_atr_status} （含大周期过滤）")
    print(f"near_low: {near_low} | near_high: {near_high}")
    print(f"MACD金叉: {golden_cross} | MACD死叉: {dead_cross}")

    # 🚀 显示 DXY 美元指数状态
    dxy_display = f"💵DXY: {dxy_trend}"
    if dxy_signal == "STRONG_UP":
        dxy_display += " 📈 强涨"
    elif dxy_signal == "STRONG_DOWN":
        dxy_display += " 📉 强跌"
    elif dxy_signal == "WEAK_UP":
        dxy_display += " 📈 弱涨"
    elif dxy_signal == "WEAK_DOWN":
        dxy_display += " 📉 弱跌"
    elif dxy_signal == "FLAT":
        dxy_display += " ➖ 横盘"

    print(f"最终信号: {entry_signal} {dxy_display} ({','.join(reason) if reason else '正常'})")
    print(f"下单状态: {order_status}")

    if stop_loss_value and take_profit_value:
        print(f"止损: {stop_loss_value:.4f} | 止盈: {take_profit_value:.4f}")
    print(f"交易锁: {'🔒 已锁定' if trade_lock else '🔓 未锁定'}")
    print("=" * 30 + "\n")


# 持仓判断（防止重复开仓）修复1：标准化币安持仓判断
def normalize_position(pos):
    if not pos:
        return None

    # ✅ 标准币安持仓格式
    if isinstance(pos, dict):
        qty = float(pos.get("positionAmt", 0))

        if abs(qty) < 1e-6:
            return None

        return "LONG" if qty > 0 else "SHORT"

    # 兼容旧逻辑（可选）
    if isinstance(pos, str):
        p = pos.upper()
        if p in ["LONG", "BUY"]:
            return "LONG"
        elif p in ["SHORT", "SELL"]:
            return "SHORT"

    return None


def monitor():
    """
    策略主监控循环（更激进开仓版）
    5秒执行一次：判断信号、检查持仓、下单、平仓
    """
    global last_ws_message_ts, last_signal, last_order_time
    global last_position_check, cached_position
    global trade_lock, trade_lock_time

    while True:
        time.sleep(5)

        # ===============================
        # 🔥 DXY 强制实时更新（无卡顿、无假数据）
        # ===============================
        try:
            from dxy_calculator import calculate_dxy, update_dxy, filtered_trend
            current_dxy_val = calculate_dxy()
            trend_raw, _, _ = update_dxy(current_dxy_val)
            dxy_signal = filtered_trend(trend_raw, confirm_n=3)
            dxy_trend = trend_raw if trend_raw else "未就绪"
        except:
            dxy_signal = None
            dxy_trend = "未就绪"

        # ===============================
        # WS 超时兜底
        # ===============================
        current_ts = time.time()
        ws_timeout = current_ts - last_ws_message_ts > WS_TIMEOUT_SECONDS
        if not ws_alive or ws_timeout or get_price() is None:
            print("⚠️ WS异常 → HTTP兜底")
            http_update()
            last_ws_message_ts = current_ts

        # ===============================
        # 读取K线与盘口
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

        # K线不足直接跳
        if len(df_1m_closed) < 20 or len(df_5m_closed) < 30 or len(df_15m_closed) < 30 or len(df_1h_closed) < 30:
            continue

        # ===============================
        # 持仓查询
        # ===============================
        try:
            if time.time() - last_position_check > 10 or cached_position is None:
                pos = get_position(SYMBOL)
                last_position_check = time.time()
                if pos is not None:
                    cached_position = pos
            position_flag = normalize_position(cached_position)
        except:
            position_flag = normalize_position(cached_position) if cached_position is not None else None

        # ===============================
        # trade_lock 优化：无持仓自动解锁
        # ===============================
        if trade_lock:
            if position_flag is None:
                print("🔓 无持仓 → 自动解锁")
                trade_lock = False
            elif time.time() - trade_lock_time > TRADE_LOCK_TIMEOUT:
                print("🔓 锁仓超时 → 自动解锁")
                trade_lock = False

        # ===============================
        # 有持仓 → 检查平仓
        # ===============================
        if position_flag:
            is_long = position_flag == "LONG"
            if should_exit_position(df_1m_closed, df_5m_closed, is_long):
                try:
                    place_order(
                        symbol=SYMBOL,
                        side="SELL" if is_long else "BUY",
                        quantity=DEFAULT_ORDER_QTY,
                        order_type="MARKET",
                        reduce_only=True
                    )
                    speak(f"平仓：{'多单' if is_long else '空单'}", key="exit", force=True)
                    trade_lock = False
                except:
                    trade_lock = False
                continue

        current_price = get_price()
        if current_price is None:
            continue

        # ===============================
        # 技术指标
        # ===============================
        indicators = calc_indicators(df_1m_closed)
        atr_1m_status = atr_filter(df_1m_closed, "1m")
        atr_5m_status = atr_filter(df_5m_closed, "5m")
        atr_15m_status = atr_filter(df_15m_closed, "15m")
        atr_1h_status = atr_filter(df_1h_closed, "1h")
        trade_status = can_trade(atr_1m_status, atr_5m_status, atr_15m_status, atr_1h_status)

        trend_5m = analyze_trend(df_5m_closed)
        trend_15m = analyze_trend(df_15m_closed)
        trend_1h = analyze_trend(df_1h_closed)

        scalping_range_status, range_high, range_low = detect_scalping_range(df_1m_closed)
        if range_low is None or range_high is None:
            scalping_range_status = "❌ 无震荡区间"

        scalping_atr_status = atr_filter_scalping(df_1m_closed)
        # ✅ 放宽大周期 ATR 限制
        if "低波动" in atr_1h_status:
            scalping_atr_status = "❌ 大周期低波动"

        golden_cross = is_macd_golden_cross(df_5m_closed)
        dead_cross = is_macd_dead_cross(df_5m_closed)

        near_low = current_price <= range_low * 1.005 if (range_low and range_high) else False
        near_high = current_price >= range_high * 0.995 if (range_low and range_high) else False

        trend_long = trend_1h in ["🟢 强多", "🟡 偏多"] and trend_15m != "🔴 强空"
        trend_short = trend_1h in ["🔴 强空", "🟠 偏空"] and trend_15m != "🟢 强多"

        trend_signal = None
        if trend_long and golden_cross and is_volume_expand(df_1m_closed):
            trend_signal = "🔵 追多"
        elif trend_short and dead_cross and is_volume_expand(df_1m_closed):
            trend_signal = "🔴 追空"

        entry_signal = get_entry_signal(current_price, scalping_range_status, scalping_atr_status,
                                        near_low, near_high, trend_signal)

        # ===============================
        # 止盈止损（异常时设置默认）
        # ===============================
        stop_loss_value = take_profit_value = None
        if indicators is not None and not np.isnan(indicators["atr"]) and indicators["atr"] > 0:
            stop_loss_value, take_profit_value = calculate_sl_tp(current_price, indicators["atr"], entry_signal)
        else:
            stop_loss_value, take_profit_value = default_sl_tp(current_price)

        # ===============================
        # reason 每次清空
        # ===============================
        reason = []

        # ===============================
        # DXY 风控（弱趋势只提示，不强制禁止）
        # ===============================
        if dxy_signal is not None:
            if "STRONG_UP" in dxy_signal and entry_signal in ["🟢 向上剃头皮", "🔵 追多"]:
                reason.append("DXY强涨 → 谨慎多")
            if "STRONG_DOWN" in dxy_signal and entry_signal in ["🟠 向下剃头皮", "🔴 追空"]:
                reason.append("DXY强跌 → 谨慎空")

        # ===============================
        # 重复信号判断
        # ===============================
        if entry_signal == last_signal:
            entry_signal = "⛔ 不交易"

        # ===============================
        # 市场状态风控
        # ===============================
        if "剃头皮" in entry_signal and "趋势市场" in trade_status:
            reason.append("趋势行情不剃头皮")
        # 只拦纯震荡，放开混合市场追单
        if "追" in entry_signal and "震荡市场" in trade_status:
            reason.append("震荡行情不追单")

        # EMA 偏离
        ema20 = df_1m_closed["close"].ewm(span=20).mean().iloc[-1]
        if abs(current_price - ema20) / current_price > 0.005:
            reason.append("偏离EMA20过远")

        # 价差风控
        if bid_price and ask_price:
            spread = (ask_price - bid_price) / bid_price
            if "剃头皮" in entry_signal and spread > 0.0008:
                reason.append(f"价差过大 {spread:.3f}")
            if "追" in entry_signal and spread > 0.0015:
                reason.append(f"价差过大 {spread:.3f}")

        # 最终拦截
        if reason:
            entry_signal = "⛔ 不交易"

        # ===============================
        # 下单
        # ===============================
        order_status = "等待信号"
        success = False

        if not trade_lock and entry_signal in ["🟢 向上剃头皮", "🔵 追多", "🟠 向下剃头皮", "🔴 追空"]:
            try:
                res = place_order(
                    symbol=SYMBOL,
                    side="BUY" if entry_signal in ["🟢 向上剃头皮", "🔵 追多"] else "SELL",
                    quantity=DEFAULT_ORDER_QTY,
                    order_type="SCALPING" if "剃头皮" in entry_signal else "NORMAL",
                    scalping_range=(range_low, range_high) if "剃头皮" in entry_signal else None,
                    stop_loss=stop_loss_value,
                    take_profit=take_profit_value
                )

                # ==============================
                # 【新增】剃头皮挂单成交校验（必加）
                # ==============================
                success = False
                if res and "orderId" in res:
                    if "剃头皮" in entry_signal:
                        # 剃头皮是 LIMIT 单，等待成交，超时撤单
                        is_filled = wait_order_filled(SYMBOL, res["orderId"], timeout=3)
                        if is_filled:
                            success = True
                        else:
                            # 未成交 → 清理本地订单
                            from order_executor import order_list
                            for idx, o in enumerate(order_list):
                                if o.get("order_id") == res["orderId"]:
                                    order_list.pop(idx)
                                    print("🧹 剃头皮挂单未成交 → 已清理本地订单")
                                    break
                    else:
                        # 追单是市价单 → 直接成功
                        success = True

            except Exception as e:
                print(f"下单异常：{e}")
                success = False


        if success:
            order_status = "✅ 下单成功"
            last_signal = entry_signal
            trade_lock = True
            trade_lock_time = time.time()
            speak(f"开仓：{entry_signal}", key="order", force=True)
            last_order_time = time.time()
        else:
            if entry_signal != "⛔ 不交易":
                order_status = "❌ 未下单"

        print_status(current_price, bid_price, ask_price, df_1m_closed, position_flag,
                     trend_5m, trend_15m, trend_1h,
                     atr_1m_status, atr_5m_status, atr_15m_status, atr_1h_status,
                     trade_status, scalping_range_status, scalping_atr_status,
                     near_low, near_high, golden_cross, dead_cross, entry_signal,
                     order_status, stop_loss_value, take_profit_value, trade_lock, reason,
                     dxy_trend, dxy_signal)


# ===============================
# 主入口
# ===============================
def main():
    global trade_lock, trade_lock_time  # 声明
    trade_lock = False
    trade_lock_time = time.time()  # 修复：初始化时间，杜绝任何边界隐患

    print("🚀 币安期货交易策略系统启动")
    sync_time()
    init_klines()
    start_ws()
    check_stop_orders(SYMBOL)
    monitor()


# 程序运行入口
if __name__ == "__main__":
    main()