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
    f"{WS_SYMBOL}@aggTrade/"  # 订阅实时成交数据
    f"{WS_SYMBOL}@markPrice/"  # 订阅标记价格（强平价格）
    f"{WS_SYMBOL}@depth20/"  # 订阅20档盘口数据
    f"{WS_SYMBOL}@kline_1m/"  # 订阅1分钟K线
    f"{WS_SYMBOL}@kline_5m/"  # 订阅5分钟K线
    f"{WS_SYMBOL}@kline_15m/"  # 订阅15分钟K线
    f"{WS_SYMBOL}@kline_30m/"  # 订阅30分钟K线
    f"{WS_SYMBOL}@kline_1h"  # 订阅1小时K线
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
            # 取最新一根K线
            new_k = data[-1]

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
    """
    WebSocket收到消息时的回调函数
    处理实时成交、价格、盘口、K线数据
    """
    # 声明全局变量
    global ws_alive, last_ws_message_ts
    # 标记WebSocket存活
    ws_alive = True
    # 更新最后消息时间
    last_ws_message_ts = time.time()
    # 解析JSON消息
    msg = json.loads(message)
    data = msg.get("data", msg)

    # 加锁处理数据，防止多线程冲突
    with lock:
        # 处理实时成交数据，更新最新价格
        if data.get("e") == "aggTrade":
            update_ws_price(float(data["p"]))
        # 处理标记价格更新
        elif data.get("e") == "markPriceUpdate":
            # 如果当前价格为空，用标记价格填充
            if get_price() is None:
                update_ws_price(float(data["p"]))
        # 处理盘口深度更新
        elif data.get("e") == "depthUpdate":
            cache["depth"]["bid"] = float(data["b"][0][0]) if data.get("b") else None
            cache["depth"]["ask"] = float(data["a"][0][0]) if data.get("a") else None
        # 处理K线数据
        elif data.get("e") == "kline":
            k = data["k"]
            interval = k["i"]
            # K线是否已收盘
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
                # 未收盘，更新最后一根K线
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
# 启动WS
# ===============================
def start_ws():
    """
    启动WebSocket长连接，使用守护线程后台运行
    断开后自动重连
    """

    def run():
        global ws_alive, last_ws_message_ts
        # 无限循环重连
        while True:
            try:
                print("🚀 启动WebSocket连接...")
                # 创建WebSocket对象，绑定回调函数
                ws = websocket.WebSocketApp(WS_URL, on_message=on_message, on_error=on_error, on_close=on_close)
                ws_alive = False
                last_ws_message_ts = time.time()
                # 启动长连接，定时发送心跳包
                ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as e:
                print(f"❌ WS异常: {e}")
            print("🔄 5秒后重连WS...")
            time.sleep(5)

    # 启动守护线程，程序退出时线程自动结束
    threading.Thread(target=run, daemon=True).start()


# ===============================
# ATR 过滤器
# ===============================
def atr_filter(df, name=""):
    """
    市场波动过滤器：波动过大/过小都禁止开仓
    用于趋势交易
    """
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

    # ATR的50周期EMA，用于判断波动趋势
    atr_mean = atr.ewm(span=50).mean().iloc[-1]

    # 异常判断
    if atr_mean == 0 or np.isnan(atr_mean):
        return f"{name} 数据异常"
    # 波动过大，禁止开仓
    if current_atr >= atr_mean * 1.5:    #這個值越大，風險越大，意味著允許的波動越大建議低於1.5
        return f"{name} ❌ 波动过大"
    # 波动过小，禁止开仓
    elif current_atr <= atr_mean * 0.5:
        return f"{name} ❌ 波动过小"
    # 波动正常，允许交易
    else:
        return f"{name} ✅ 波动正常"


# ===============================
# 震荡区间识别
# ===============================
def detect_scalping_range(df):
    """
    剃头皮震荡识别（增强版）?????????????????????????目前這個函數有問題，是否保留，或者改爲，計算若干跟K綫的平均值，然後判斷在平均值上面，和下面的次數比
    严格判断是否处于适合高抛低吸的震荡区间
    返回：
        状态, 区间高点, 区间低点
    """

    if len(df) < 60:
        return "数据不足", None, None

    # ===== 1. 取最近60根K线 =====
    recent = df.tail(60)

    # 区间最高点
    range_high = recent["high"].max()
    # 区间最低点
    range_low = recent["low"].min()
    # 均价
    mean_price = recent["close"].mean()

    # ===== 2. 波动率判断 =====
    range_pct = (range_high - range_low) / range_low

    # 增加标准差和ATR判断
    std_pct = recent["close"].std() / mean_price
    high_low = recent["high"] - recent["low"]
    high_close = (recent["high"] - recent["close"].shift(1)).abs()
    low_close = (recent["low"] - recent["close"].shift(1)).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    atr = tr.rolling(14).mean().iloc[-1]
    atr_pct = atr / mean_price

    # 波动超过阈值，不是震荡
    """
    range_pct	0.005	最近60根K线最高点-最低点 / 最低点	        白银1小时波动大约在0.3%-0.5%，所以可以调高一点，0.006~0.008比较合理
    std_pct	0.0035	    最近K线收盘价标准差 / 均价             	    反映价格离散程度，白银短线容易被微波动干扰，可调0.004~0.005
    atr_pct	0.004	    最近K线ATR平均值 / 均价	                ATR对短线敏感，可以稍微放宽，0.0045~0.0055
    """

    if range_pct > 0.028 or std_pct > 0.025 or atr_pct > 0.019:
        return "非震荡（波动过大）", None, None

    # ===== 3. 趋势过滤（防止趋势中误判震荡） =====
    ema20 = recent["close"].ewm(span=20).mean()
    slope = ema20.iloc[-1] - ema20.iloc[-5]

    if abs(slope) > mean_price * 0.001:
        return "趋势中（禁止剃头皮）", None, None

    # ===== 4. 波动是否在收敛 =====
    recent_range = recent["high"] - recent["low"]
    if recent_range.iloc[-1] > recent_range.mean() * 1.2:
        return "波动放大中", None, None

    # ===== 5. 触碰高低点次数判断 =====
    touch_high = (recent["high"] >= range_high * 0.9995).sum()
    touch_low = (recent["low"] <= range_low * 1.0005).sum()

    if touch_high < 2 or touch_low < 2:
        return "震荡不足", None, None

    # ===== 6. 假突破过滤 =====
    last_close = recent["close"].iloc[-1]
    if last_close > range_high * 1.001 or last_close < range_low * 0.999:
        return "已突破区间", None, None

    # ===== 7. 中心回归检测 =====
    """

    deviation.mean()    0.002      最近K线收盘价平均偏离区间均价比例（0.2%）
    白银波动大，这个阈值偏小，容易误判偏离过大。可调到 0.003 ~ 0.005（0.3%~0.5%）
"""
    deviation = abs(recent["close"] - mean_price) / mean_price
    if deviation.mean() > 0.005:
        return "偏离中心过大", None, None

    # ===== ✅ 最终判定：适合剃头皮 =====
    return "🟡 可剃头皮", range_high, range_low


# ===============================
# 剃头皮ATR
# ===============================
def atr_filter_scalping(df):
    """
    剃头皮专用ATR过滤（增强版）
    波动必须在合理区间才允许剃头皮
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

    if len(df) < 25:
        return False

    vol = df["volume"]

    # ===== 1. 当前已收盘成交量 =====
    current_vol = vol.iloc[-2]

    # ===== 2. 过去20根均量（不含当前）=====
    avg_vol = vol.iloc[-22:-2].mean()

    # ===== 3. 基础放量判断 =====
    is_expand = current_vol > avg_vol * 1.4

    # ===== 4. 连续放量确认 =====
    prev_vol = vol.iloc[-3]
    confirm = current_vol > prev_vol

    # ===== 5. 排除极端异常成交量 =====
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
    根据ATR波动过滤结果，判断是否允许交易
    任意周期波动异常 → 禁止交易
    """
    reasons = []
    # 检查1分钟波动
    if "过大" in atr_1m_status:
        reasons.append("1m波动过大")
    if "过小" in atr_1m_status:
        reasons.append("1m波动过小")
    # 检查5分钟波动
    if "过大" in atr_5m_status:
        reasons.append("5m波动过大")
    if "过小" in atr_5m_status:
        reasons.append("5m波动过小")
    # 检查15分钟波动
    if "过大" in atr_15m_status:
        reasons.append("15m波动过大")
    if "过小" in atr_15m_status:
        reasons.append("15m波动过小")
    # 检查1小时波动
    if "过大" in atr_1h_status:
        reasons.append("1h波动过大")
    if "过小" in atr_1h_status:
        reasons.append("1h波动过小")

    # 如果有禁止原因，返回禁止
    if reasons:
        return f"❌ 禁止交易（{'，'.join(reasons)}）"
    # 全部正常，返回允许
    return "✅ 可以交易"


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

    return stop_loss, take_profit


def print_status(current_price, bid_price, ask_price, df_1m_closed, position_flag,
                 trend_5m, trend_15m, trend_1h,
                 atr_1m_status, atr_5m_status, atr_15m_status, atr_1h_status,  # 新增15m+1h ATR
                 trade_status, scalping_range_status, scalping_atr_status,
                 near_low, near_high, golden_cross, dead_cross, entry_signal,
                 order_status, stop_loss_value, take_profit_value, trade_lock, reason=None):
    """
    控制台打印实时策略状态，方便监控
    【已修复：周期一致性 + 4周期ATR显示】
    """
    print("\n" + "=" * 30)
    # 打印当前价格
    print(f"📊 实时数据 - 价格: {current_price:.4f}")
    # 打印盘口买卖一
    bid_str = f"{bid_price:.4f}" if bid_price else "N/A"
    ask_str = f"{ask_price:.4f}" if ask_price else "N/A"
    print(f"盘口 - 买一: {bid_str} | 卖一: {ask_str}")
    # 打印成交量信息
    print(f"成交量均值: {df_1m_closed['volume'].mean():.2f} | 最新成交量: {df_1m_closed['volume'].iloc[-1]:.2f}")
    # 打印持仓状态
    print(f"持仓状态: {'✅ 有持仓' if position_flag else '❌ 无持仓'}")

    # ====================== 修复：周期一致性判断 ======================
    trend_list = [trend_5m, trend_15m, trend_1h]
    valid_trends = [t for t in trend_list if t not in ["⚪ 震荡", "数据不足"]]

    if len(valid_trends) <= 1:
        # 只有一个周期有方向 → 不算一致
        period_consistency = "❌ 不一致"
    else:
        # 多个周期必须完全同向才算一致
        first = valid_trends[0]
        all_same = all(t == first for t in valid_trends)
        period_consistency = "✅ 一致" if all_same else "❌ 不一致"

    # 打印多周期趋势
    print("\n📈 多周期趋势")
    print(f"5分钟: {trend_5m} | 15分钟: {trend_15m} | 1小时: {trend_1h}")
    print(f"周期一致性: {period_consistency}")

    # 打印ATR波动过滤（4周期完整显示）
    print("\n🛡️ ATR波动过滤")
    print(f"1m: {atr_1m_status} | 5m: {atr_5m_status} | 15m: {atr_15m_status} | 1h: {atr_1h_status}")
    print(f"最终交易状态: {trade_status}")

    # 打印策略信号
    print("\n🎯 策略信号")
    print(f"震荡区间状态: {scalping_range_status}")
    print(f"剃头皮ATR状态: {scalping_atr_status}")
    print(f"near_low: {near_low} | near_high: {near_high}")
    print(f"MACD金叉: {golden_cross} | MACD死叉: {dead_cross}")
    print(f"最终信号: {entry_signal} {'(' + ','.join(reason) + ')' if reason else ''}")
    print(f"下单状态: {order_status}")
    # 打印止盈止损
    if stop_loss_value and take_profit_value:
        print(f"止损: {stop_loss_value:.4f} | 止盈: {take_profit_value:.4f}")
    # 打印交易锁状态
    print(f"交易锁: {'🔒 已锁定' if trade_lock else '🔓 未锁定'}")
    print("=" * 30 + "\n")


def monitor():
    """
    策略主监控循环
    5秒执行一次：判断信号、检查持仓、下单、平仓
    """
    global last_ws_message_ts, last_signal, last_order_time
    global last_position_check, cached_position
    global trade_lock

    while True:
        time.sleep(5)

        # 如果交易锁开启，跳过本次循环
        if trade_lock:
            time.sleep(1)
            continue

        current_ts = time.time()

        # WebSocket超时判断，触发HTTP兜底
        ws_timeout = current_ts - last_ws_message_ts > WS_TIMEOUT_SECONDS
        if not ws_alive or ws_timeout or get_price() is None:
            print("⚠️ WS异常 → 自动恢复中（HTTP兜底）")
            http_update()
            last_ws_message_ts = current_ts

        # 加锁读取缓存数据
        with lock:
            df_1m = pd.DataFrame(cache["klines"]["1m"])
            df_5m = pd.DataFrame(cache["klines"]["5m"])
            df_15m = pd.DataFrame(cache["klines"]["15m"])
            df_1h = pd.DataFrame(cache["klines"]["1h"])
            bid_price = cache["depth"]["bid"]
            ask_price = cache["depth"]["ask"]

        # 只使用已收盘K线计算指标，无未来函数
        df_1m_closed = df_1m.iloc[:-1]
        df_5m_closed = df_5m.iloc[:-1]
        df_15m_closed = df_15m.iloc[:-1]
        df_1h_closed = df_1h.iloc[:-1]

        # 无数据时跳过
        if df_1m_closed.empty:
            continue

        # 持仓信息缓存，10秒更新一次，防API高频
        try:
            if time.time() - last_position_check > 10:
                cached_position = get_position(SYMBOL)
                last_position_check = time.time()
            position_flag = cached_position
        except Exception as e:
            print(f"⚠️ 持仓判断失败：{e}")
            continue

        # 有持仓时，检查平仓条件
        if position_flag:
            is_long = position_flag == "LONG"
            if should_exit_position(df_1m_closed, df_5m_closed, is_long):
                try:
                    # 市价平仓
                    success = place_order(
                        symbol=SYMBOL,
                        side="SELL" if is_long else "BUY",
                        quantity=DEFAULT_ORDER_QTY,
                        order_type="MARKET",
                        reduce_only=True
                    )
                    if success:
                        speak(f"已执行主动平仓，{'多单平仓' if is_long else '空单平仓'}", key="exit", force=True)
                        trade_lock = False
                    continue
                except Exception as e:
                    print(f"⚠️ 平仓失败: {e}")
                    trade_lock = False
                    continue

        # 获取当前最新价格
        current_price = get_price()
        if current_price is None:
            continue

        # 计算指标与状态
        indicators = calc_indicators(df_1m_closed)
        # ====================== 修复：ATR 增加 15m + 1h ======================
        atr_1m_status = atr_filter(df_1m_closed, "1m")
        atr_5m_status = atr_filter(df_5m_closed, "5m")
        atr_15m_status = atr_filter(df_15m_closed, "15m")  # 新增
        atr_1h_status = atr_filter(df_1h_closed, "1h")  # 新增
        # 4周期ATR判断是否可交易
        trade_status = can_trade(atr_1m_status, atr_5m_status, atr_15m_status, atr_1h_status)

        # 各周期趋势判断
        trend_5m = analyze_trend(df_5m_closed)
        trend_15m = analyze_trend(df_15m_closed)
        trend_1h = analyze_trend(df_1h_closed)

        # 震荡区间判断
        scalping_range_status, range_high, range_low = detect_scalping_range(df_1m_closed)
        # 剃头皮ATR判断
        scalping_atr_status = atr_filter_scalping(df_1m_closed)
        # MACD交叉信号
        golden_cross = is_macd_golden_cross(df_5m_closed)
        dead_cross = is_macd_dead_cross(df_5m_closed)

        # 判断价格是否靠近震荡区间高低点
        near_low = current_price <= range_low * 0.995 if (range_low and range_high) else False
        near_high = current_price >= range_high * 1.005 if (range_low and range_high) else False

        # 趋势交易信号判断
        trend_long = trend_1h in ["🟢 强多", "🟡 偏多"] and trend_15m != "🔴 强空"
        trend_short = trend_1h in ["🔴 强空", "🟠 偏空"] and trend_15m != "🟢 强多"
        trend_signal = None
        if trend_long and golden_cross and is_low_volume(df_1m_closed) and is_volume_expand(df_1m_closed):
            trend_signal = "🔵 追多"
        elif trend_short and dead_cross and is_low_volume(df_1m_closed) and is_volume_expand(df_1m_closed):
            trend_signal = "🔴 追空"

        # 生成最终开仓信号
        entry_signal = get_entry_signal(current_price, scalping_range_status, scalping_atr_status, near_low, near_high,
                                        trend_signal)

        # 额外风控条件
        reason = []
        ema20 = df_1m_closed["close"].ewm(span=20).mean().iloc[-1]
        # 价格偏离EMA20过大，不追单
        if abs(current_price - ema20) / current_price > 0.01:
            entry_signal = "⛔ 偏离过大，不追"
            reason.append("EMA20偏离过大")
        # ATR风控禁止交易
        if "禁止交易" in trade_status:
            entry_signal = "⛔ ATR风控：禁止交易"
            reason.append(atr_1m_status + " | " + atr_5m_status)
        # 盘口价差过大，不交易
        if bid_price and ask_price and (ask_price - bid_price) / bid_price > 0.001:
            entry_signal = "⛔ 盘口价差过大，不交易"
            reason.append(f"价差 {(ask_price - bid_price) / bid_price:.4f}")

        # 计算止盈止损
        stop_loss_value = take_profit_value = None
        if indicators and "atr" in indicators:
            stop_loss_value, take_profit_value = calculate_sl_tp(current_price, indicators["atr"], entry_signal)

        # 执行下单
        order_status = "未下单"
        try:
            if entry_signal in ["🟢 向上剃头皮", "🔵 追多"]:
                success = place_order(
                    symbol=SYMBOL, side="BUY", quantity=DEFAULT_ORDER_QTY,
                    order_type="SCALPING" if "剃头皮" in entry_signal else "NORMAL",
                    scalping_range=(range_low, range_high) if "剃头皮" in entry_signal else None,
                    stop_loss=stop_loss_value, take_profit=take_profit_value
                )
            elif entry_signal in ["🟠 向下剃头皮", "🔴 追空"]:
                success = place_order(
                    symbol=SYMBOL, side="SELL", quantity=DEFAULT_ORDER_QTY,
                    order_type="SCALPING" if "剃头皮" in entry_signal else "NORMAL",
                    scalping_range=(range_low, range_high) if "剃头皮" in entry_signal else None,
                    stop_loss=stop_loss_value, take_profit=take_profit_value
                )
            else:
                success = False

            if success:
                order_status = "✅ 下单成功"
                last_signal = entry_signal
                last_order_time = time.time()
                trade_lock = True  # 开启交易锁
                speak(f"已下单：{entry_signal}", key="order", force=True)
            else:
                if entry_signal != "⛔ 不交易":
                    order_status = "❌ 未下单"
                    trade_lock = False
        except Exception as e:
            print(f"⚠️ 下单异常: {e}")
            trade_lock = False

        # 打印实时状态（已传入4个周期ATR）
        print_status(current_price, bid_price, ask_price, df_1m_closed, position_flag,
                     trend_5m, trend_15m, trend_1h,
                     atr_1m_status, atr_5m_status, atr_15m_status, atr_1h_status,
                     trade_status, scalping_range_status, scalping_atr_status,
                     near_low, near_high, golden_cross, dead_cross, entry_signal,
                     order_status, stop_loss_value, take_profit_value, trade_lock, reason)


# ===============================
# 主入口
# ===============================
def main():
    """
    程序主入口
    依次执行：时间同步→初始化K线→启动WebSocket→检查挂单→启动策略监控
    """
    print("🚀 币安期货交易策略系统启动")
    sync_time()  # 同步本地时间与币安服务器时间
    init_klines()  # 初始化历史K线数据
    start_ws()  # 启动WebSocket实时数据接收
    check_stop_orders(SYMBOL)  # 检查未成交挂单
    monitor()  # 启动策略主循环


# 程序运行入口
if __name__ == "__main__":
    main()