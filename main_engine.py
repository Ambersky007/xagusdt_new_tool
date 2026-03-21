# 导入所需的核心库
import requests  # 用于发送HTTP请求获取币安API数据
import websocket  # 用于建立WebSocket连接接收实时行情
import json  # 用于解析JSON格式的数据
import threading  # 用于创建多线程（WebSocket和监控分开运行）
import time  # 用于设置时间间隔、休眠等操作
import pandas as pd  # 用于数据处理和分析（核心数据结构DataFrame）
import numpy as np  # 用于数值计算（如处理NaN值、数学运算）
from collections import deque  # 用于创建固定长度的队列（缓存K线数据）
from order_executor import place_order, check_stop_orders
from order_executor import place_order
from config_account import DEFAULT_ORDER_QTY, SYMBOL
from voice_alert import speak


# ===============================
# 配置区 - 策略核心参数配置
# ===============================
SYMBOL = "XAGUSDT"  # 交易对：白银/USDT
WS_SYMBOL = SYMBOL.lower()  # WebSocket使用小写的交易对名称（币安要求）

HTTP_BASE = "https://fapi.binance.com"  # 币安期货HTTP接口基础地址

# ✅ 多流（官方标准）：同时订阅多个实时数据流，用/分隔
# aggTrade: 聚合交易数据 | markPrice: 标记价格 | depth20: 20档深度 | kline_1m: 1分钟K线
WS_URL = f"wss://fstream.binance.com/stream?streams={WS_SYMBOL}@aggTrade/{WS_SYMBOL}@markPrice/{WS_SYMBOL}@depth20/{WS_SYMBOL}@kline_1m"

INTERVALS = ["1m", "5m", "15m", "30m", "1h"]  # 需要监控的K线周期
INIT_LIMIT = 120  # 初始化时获取的历史K线数量

# ===============================
# 数据缓存区 - 存储实时和历史数据（全局共享）
# ===============================
cache = {
    "price": None,  # 最新成交价
    "depth": None,  # 盘口深度数据
    "klines": {i: deque(maxlen=200) for i in INTERVALS},  # 各周期K线队列（固定长度200，自动淘汰旧数据）
}

lock = threading.Lock()  # 线程锁：防止多线程同时修改cache导致数据错乱
ws_alive = False  # WebSocket连接状态标记


# ===============================
# 初始化K线数据（程序启动时执行）
# 功能：从币安HTTP接口获取各周期的历史K线数据，填充到缓存
# ===============================
def init_klines():
    print("📥 初始化K线...")

    # 遍历所有需要监控的K线周期
    for interval in INTERVALS:
        try:
            # 构造获取K线的HTTP接口地址
            url = f"{HTTP_BASE}/fapi/v1/klines"
            # 请求参数：交易对、周期、获取数量
            params = {"symbol": SYMBOL, "interval": interval, "limit": INIT_LIMIT}

            # 发送GET请求获取K线数据（超时时间10秒）
            data = requests.get(url, params=params, timeout=10).json()

            # 加锁修改缓存（防止多线程冲突）
            with lock:
                cache["klines"][interval].clear()  # 清空原有数据
                # 遍历返回的K线数据，格式化后存入队列
                for k in data:
                    cache["klines"][interval].append({
                        "time": k[0],  # K线开始时间戳
                        "open": float(k[1]),  # 开盘价（转换为浮点数）
                        "high": float(k[2]),  # 最高价
                        "low": float(k[3]),  # 最低价
                        "close": float(k[4]),  # 收盘价
                        "volume": float(k[5])  # 成交量
                    })

            print(f"✅ {interval} OK")  # 初始化成功提示

        except Exception as e:
            # 捕获异常并打印（防止单个周期失败导致整个程序崩溃）
            print(f"❌ {interval} 初始化失败:", e)


# ===============================
# HTTP兜底更新函数
# 功能：当WebSocket断开时，通过HTTP接口更新K线和价格数据
# ===============================
def http_update():
    # K线更新接口地址
    url = f"{HTTP_BASE}/fapi/v1/klines"

    # 遍历所有周期更新K线
    for interval in INTERVALS:
        try:
            # 获取缓存中该周期最后一根K线的时间
            last_time = cache["klines"][interval][-1]["time"]

            # 请求最新的2根K线（用于判断是否有新K线生成）
            params = {"symbol": SYMBOL, "interval": interval, "limit": 2}
            data = requests.get(url, params=params, timeout=5).json()

            new_k = data[-1]  # 取最新的一根K线

            # 如果最新K线时间与缓存最后一根不同，说明有新K线
            if new_k[0] != last_time:
                with lock:
                    # 将新K线添加到缓存队列
                    cache["klines"][interval].append({
                        "time": new_k[0],
                        "open": float(new_k[1]),
                        "high": float(new_k[2]),
                        "low": float(new_k[3]),
                        "close": float(new_k[4]),
                        "volume": float(new_k[5])
                    })

        except:
            # 静默失败：单个周期更新失败不影响其他周期
            pass

    # ✅ 价格兜底：更新最新成交价
    try:
        # 请求最新成交价接口
        r = requests.get(f"{HTTP_BASE}/fapi/v1/ticker/price",
                         params={"symbol": SYMBOL}, timeout=3)
        with lock:
            # 解析并存储最新价格
            cache["price"] = float(r.json()["price"])
    except:
        pass


# ===============================
# 技术指标计算函数
# 功能：基于K线数据计算MACD、KDJ、ATR等核心技术指标
# 参数：df - K线数据的DataFrame
# 返回：包含各指标最新值的字典
# ===============================
def calc_indicators(df):
    # 数据量不足30根，无法计算有效指标，返回None
    if len(df) < 30:
        return None

    close = df["close"]  # 提取收盘价序列

    # 计算MACD指标
    ema12 = close.ewm(span=12).mean()  # 12日指数移动平均线
    ema26 = close.ewm(span=26).mean()  # 26日指数移动平均线
    macd = ema12 - ema26  # MACD快线
    signal = macd.ewm(span=9).mean()  # MACD慢线（信号线）

    # 计算KDJ指标
    low_min = df["low"].rolling(9).min()  # 9周期最低价最小值
    high_max = df["high"].rolling(9).max()  # 9周期最高价最大值
    rsv = (close - low_min) / (high_max - low_min + 1e-9) * 100  # 未成熟随机值（+1e-9防止除零）

    k = rsv.ewm(com=2).mean()  # K值（平滑RSV）
    d = k.ewm(com=2).mean()  # D值（平滑K值）
    j = 3 * k - 2 * d  # J值（3K-2D）

    # 计算ATR指标（平均真实波幅）
    atr = (df["high"] - df["low"]).rolling(14).mean()  # 14周期高低价差的平均值

    # 返回各指标的最新值（最后一行）
    return {
        "macd": macd.iloc[-1],
        "signal": signal.iloc[-1],
        "k": k.iloc[-1],
        "d": d.iloc[-1],
        "j": j.iloc[-1],
        "atr": atr.iloc[-1]
    }


# ===============================
# 多周期趋势分析函数
# 功能：基于EMA和MACD判断当前趋势方向（多/空/震荡）
# 参数：df - K线数据的DataFrame
# 返回：趋势描述字符串
# ===============================
def analyze_trend(df):
    # 数据量不足30根K线，返回"数据不足"
    if len(df) < 30:
        return "数据不足"

    close = df["close"]  # 提取收盘价列

    # 计算12周期EMA、26周期EMA及MACD指标
    ema12 = close.ewm(span=12).mean()  # 12周期指数移动平均线
    ema26 = close.ewm(span=26).mean()  # 26周期指数移动平均线
    macd = ema12 - ema26  # MACD快线（DIF）
    signal = macd.ewm(span=9).mean()  # MACD慢线（DEA/信号线）

    # 提取最近3根K线的指标值，用于判断趋势延续性
    last_n = 3
    scores = []  # 存储最近3根K线的趋势评分

    # 遍历最近3根K线（从倒数第3根到倒数第1根）
    for i in range(-last_n, 0):
        score = 0  # 单根K线的趋势评分初始化
        # 1. EMA趋势判断：EMA12高于EMA26则看多，加1分；反之看空，减1分
        if ema12.iloc[i] > ema26.iloc[i]:
            score += 1
        else:
            score -= 1
        # 2. MACD趋势判断：MACD快线高于慢线则看多，加1分；反之看空，减1分
        if macd.iloc[i] > signal.iloc[i]:
            score += 1
        else:
            score -= 1
        # 3. 价格位置判断：收盘价高于EMA12则看多，加1分；反之看空，减1分
        if close.iloc[i] > ema12.iloc[i]:
            score += 1
        else:
            score -= 1
        scores.append(score)  # 将单根K线评分加入列表

    # 根据连续3根K线的评分判断趋势延续性
    if all(s >= 2 for s in scores):
        return "🟢 强多"  # 连续3根K线评分均≥2：强势多头趋势（延续性强）
    elif all(s == 1 for s in scores):
        return "🟡 偏多"  # 连续3根K线评分均为1：偏弱多头趋势（延续性一般）
    elif all(s <= -2 for s in scores):
        return "🔴 强空"  # 连续3根K线评分均≤-2：强势空头趋势（延续性强）
    elif all(s == -1 for s in scores):
        return "🟠 偏空"  # 连续3根K线评分均为-1：偏弱空头趋势（延续性一般）
    else:
        return "⚪ 震荡"  # 评分不一致/存在分歧：无明确趋势，判定为震荡行情

# ===============================
# WebSocket相关函数
# 功能：建立实时数据连接，处理实时行情推送
# ===============================
def on_message(ws, message):
    global ws_alive
    ws_alive = True  # 标记WebSocket连接正常

    # 解析推送的JSON数据
    msg = json.loads(message)

   #解析combined stream（多流合并推送的数据格式）
    data = msg.get("data", msg)

    # 加锁修改缓存
    with lock:
        # ===============================
        # 处理聚合交易数据（最新成交价）
        # ===============================
        if data.get("e") == "aggTrade":
            cache["price"] = float(data["p"])  # 更新最新成交价

        # ===============================
        # 处理标记价格更新（备用价格源）
        # ===============================
        elif data.get("e") == "markPriceUpdate":
            # 只有当主价格为空时才更新（作为兜底）
            if cache["price"] is None:
                cache["price"] = float(data["p"])

        # ===============================
        # 处理深度更新（盘口数据）
        # ===============================
        elif data.get("e") == "depthUpdate":
            cache["depth"] = data  # 存储深度数据

        # ===============================
        # 处理1分钟K线更新
        # ===============================
        elif data.get("e") == "kline":
            k = data["k"]  # 提取K线数据

            # 格式化K线数据
            new_k = {
                "time": k["t"],  # K线开始时间
                "open": float(k["o"]),  # 开盘价
                "high": float(k["h"]),  # 最高价
                "low": float(k["l"]),  # 最低价
                "close": float(k["c"]),  # 收盘价
                "volume": float(k["v"])  # 成交量
            }

            # 检查是否是新K线（避免重复添加）
            if cache["klines"]["1m"][-1]["time"] != new_k["time"]:
                cache["klines"]["1m"].append(new_k)  # 添加到1分钟K线队列


def on_error(ws, error):
    # WebSocket错误处理
    global ws_alive
    ws_alive = False  # 标记连接异常
    print("❌ WS错误:", error)  # 打印错误信息


def on_close(ws, *args):
    # WebSocket关闭处理
    global ws_alive
    ws_alive = False  # 标记连接关闭
    print("⚠️ WS关闭")  # 打印关闭提示


def start_ws():
    # 启动WebSocket连接
    print("🚀 启动WS...")

    # 创建WebSocket应用实例
    ws = websocket.WebSocketApp(
        WS_URL,
        on_message=on_message,  # 消息处理函数
        on_error=on_error,  # 错误处理函数
        on_close=on_close  # 关闭处理函数
    )

    # 创建守护线程运行WebSocket（不阻塞主线程）
    threading.Thread(target=ws.run_forever, daemon=True).start()


# ===============================
# ATR波动过滤器（双周期）
# 功能：判断当前波动是否在合理范围（过大/过小/正常）
# 参数：df - K线DataFrame, name - 周期名称（用于打印）
# 返回：波动状态描述
# ===============================
def atr_filter(df, name=""):
    # 数据量不足50根，返回数据不足
    if len(df) < 50:
        return f"{name} 数据不足"

    # 计算14周期ATR
    atr = (df["high"] - df["low"]).rolling(14).mean()
    current_atr = atr.iloc[-1]  # 当前ATR值
    atr_mean = atr.rolling(50).mean().iloc[-1]  # 50周期ATR均值

    # 防止除零或NaN值
    if atr_mean == 0 or np.isnan(atr_mean):
        return f"{name} 数据异常"

    # 判断波动范围
    if current_atr >= atr_mean * 2:
        return f"{name} ❌ 波动过大"  # 波动超过均值2倍：禁止交易
    elif current_atr <= atr_mean * 0.5:
        return f"{name} ❌ 波动过小"  # 波动低于均值0.5倍：禁止交易
    else:
        return f"{name} ✅ 波动正常"  # 波动正常：允许交易


# ===============================
# 1分钟周期剃头皮震荡识别，取了15根K线，判断的是15分钟内的震荡特征
# 功能：判断是否处于适合剃头皮（超短线）的震荡区间
# 参数：df - 1分钟K线DataFrame
# 返回：震荡状态、区间高点、区间低点
# ===============================
def detect_scalping_range(df):
    # 数据量不足20根，返回数据不足
    if len(df) < 20:
        return "数据不足", None, None

    recent = df.tail(15)  # 取最近15根K线

    # 计算区间高低点和平均价格
    high = recent["high"].max()  # 区间高点
    low = recent["low"].min()  # 区间低点
    mean_price = recent["close"].mean()  # 区间平均收盘价

    # 计算区间幅度百分比（控制在0.2%以内）
    range_pct = (high - low) / mean_price

    # 区间幅度超过0.2%，不是震荡行情
    if range_pct > 0.002:        #这个值是剥头皮的阈值，需要考虑5分钟K线的振幅，最好做个对比？2%有点低对于这几天的行情
        return "非震荡（波动过大）", None, None

    # 计算边界触碰次数（判断是否有效震荡区间）
    touch_high = (recent["high"] > high * 0.999).sum()  # 触碰高点次数
    touch_low = (recent["low"] < low * 1.001).sum()  # 触碰低点次数

    # 高低点都触碰至少2次，判定为可剃头皮的震荡区间
    if touch_high >= 2 and touch_low >= 2:
        return "🟡 可剃头皮", high, low

    # 震荡不足，不适合剃头皮
    return "震荡不足", None, None


# ===============================
# 剃头皮专用ATR过滤
# 功能：针对剃头皮策略的ATR波动过滤
# 参数：df - 1分钟K线DataFrame
# 返回：波动状态
# ===============================
def atr_filter_scalping(df):
    # 计算14周期ATR
    atr = (df["high"] - df["low"]).rolling(14).mean()

    current = atr.iloc[-1]  # 当前ATR
    mean = atr.rolling(50).mean().iloc[-1]  # 50周期ATR均值

    # 判断波动是否适合剃头皮
    if current >= mean * 1.8:          #当前市场的波动幅度，达到了近期正常波动水平的 1.8 倍及以上
        return "❌ 波动过大"
    elif current <= mean * 0.6:            #当前市场的波动幅度，低于了近期正常波动水平的 0.6 倍及一下
        return "❌ 波动过小"
    else:
        return "✅ 可剃头皮"


# ===============================
# 低成交量判断                        （和atr_filter_scalping(df)协同作用，双标准判断）
# 功能：判断最新成交量是否处于近期低位（趋势启动前的缩量）
# 参数：df - K线DataFrame
# 返回：布尔值（是否低成交量）
# ===============================
def is_low_volume(df):
    # 数据量不足60根，返回False
    if len(df) < 60:
        return False

    vol = df["volume"]  # 成交量序列
    last_vol = vol.iloc[-2]  # 倒数第二根K线成交量（避免最新未完成K线）

    lowest_5 = vol.nsmallest(5)  # 最近60根中成交量最小的5个值
    avg_low = lowest_5.mean()  # 最小5个值的平均值

    # 判断最新成交量是否在低位区间（85%-115%）
    return avg_low * 0.85 <= last_vol <= avg_low * 1.15


# ===============================
# 成交量放量判断
# 功能：判断最新成交量是否放量（趋势启动信号）
# 参数：df - K线DataFrame
# 返回：布尔值（是否放量）
# ===============================
def is_volume_expand(df):
    vol = df["volume"]  # 成交量序列
    # 最新成交量 > 前一根3倍，判定为放量
    return vol.iloc[-1] > vol.iloc[-2] * 3         #这个参数最好根据周期波动值调整。主力启动的时候一半会放量3倍


# ===============================
# MACD金叉检测（5分钟周期）
# 功能：判断MACD快线是否上穿慢线（多头启动信号）
# 参数：df - 5分钟K线DataFrame
# 返回：布尔值（是否金叉）
# ===============================
def is_macd_golden_cross(df):
    close = df["close"]  # 收盘价序列

    # 计算MACD
    ema12 = close.ewm(span=12).mean()
    ema26 = close.ewm(span=26).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9).mean()

    # 数据量不足2根，返回False
    if len(macd) < 2:
        return False

    # 金叉条件：前一根MACD < 信号线，最新MACD > 信号线
    return macd.iloc[-2] < signal.iloc[-2] and macd.iloc[-1] > signal.iloc[-1]


# ===============================
# MACD死叉检测（5分钟周期）
# 功能：判断MACD快线是否下穿慢线（空头启动信号）
# 参数：df - 5分钟K线DataFrame
# 返回：布尔值（是否死叉）
# ===============================
def is_macd_dead_cross(df):
    close = df["close"]  # 收盘价序列

    # 计算MACD
    ema12 = close.ewm(span=12).mean()
    ema26 = close.ewm(span=26).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9).mean()

    # 数据量不足2根，返回False
    if len(macd) < 2:
        return False

    # 死叉条件：前一根MACD > 信号线，最新MACD < 信号线
    return macd.iloc[-2] > signal.iloc[-2] and macd.iloc[-1] < signal.iloc[-1]


# ===============================
# ATR最终交易判断函数
# 功能：综合双周期ATR结果，判断是否允许交易
# 参数：atr_1m_status - 1分钟ATR状态, atr_5m_status - 5分钟ATR状态
# 返回：交易状态
# ===============================
def can_trade(atr_1m_status, atr_5m_status):
    if "过大" in atr_1m_status:
        return "❌ 禁止交易（短期波动过大）"
    if "过小" in atr_5m_status:
        return "❌ 禁止交易（市场太平）"
    return "✅ 可以交易"


# ===============================
# 监控主函数
# 功能：循环监控市场数据，计算策略信号并输出
# ===============================
def monitor():
    # 无限循环监控
    while True:
        time.sleep(5)  # 每5秒执行一次

        # WS挂了 → HTTP兜底更新数据
        if not ws_alive:
            print("⚠️ WS不可用 → HTTP接管")
            http_update()

        # 价格仍然没有 → 强制HTTP更新
        if cache["price"] is None:
            http_update()

        # 加锁读取缓存数据（防止读取时被修改）
        with lock:
            df_1m = pd.DataFrame(cache["klines"]["1m"])  # 1分钟K线DataFrame
            df_5m = pd.DataFrame(cache["klines"]["5m"])  # 5分钟K线DataFrame
            df_15m = pd.DataFrame(cache["klines"]["15m"])  # 15分钟K线DataFrame
            df_1h = pd.DataFrame(cache["klines"]["1h"])  # 1小时K线DataFrame

        # 1分钟数据为空，跳过本次循环
        if df_1m.empty:
            continue

        # 计算1分钟技术指标
        indicators = calc_indicators(df_1m)
        vol = df_1m["volume"]  # 1分钟成交量序列

        # -----------------------------
        # ATR双周期过滤
        # -----------------------------
        atr_1m = atr_filter(df_1m, "1m")  # 1分钟ATR过滤
        atr_5m = atr_filter(df_5m, "5m")  # 5分钟ATR过滤

        # 最终交易状态
        trade_status = can_trade(atr_1m, atr_5m)

        # 打印实时数据
        print("\n====== 实时数据 ======")
        print("价格:", cache["price"])
        print("成交量均值:", round(vol.mean(), 2))
        print("最大成交量:", round(vol.max(), 2))
        print("最小成交量:", round(vol.min(), 2))

        # 分析各周期趋势
        trend_5m = analyze_trend(df_5m)
        trend_15m = analyze_trend(df_15m)
        trend_1h = analyze_trend(df_1h)

        # ===============================
        # 🎯 策略引擎（核心）
        # 整合所有条件，生成最终交易信号
        # ===============================

        # ===== 1️⃣ 剃头皮判断 =====
        # 判断是否处于可剃头皮的震荡区间
        range_status, range_high, range_low = detect_scalping_range(df_1m)
        # 剃头皮专用ATR过滤
        atr_status = atr_filter_scalping(df_1m)

        # 剃头皮信号：震荡区间 + ATR正常
        scalp_signal = (
                "可剃头皮" in range_status and
                "可剃头皮" in atr_status
        )

        # ===== 边界判断（剃头皮必须条件）
        price = df_1m["close"].iloc[-1]  # 最新收盘价

        near_low = False  # 是否靠近区间低点
        near_high = False  # 是否靠近区间高点

        # 如果有有效震荡区间
        if range_low and range_high:
            near_low = price <= range_low * 0.999  # 价格 <= 低点*0.999
            near_high = price >= range_high * 1.001  # 价格 >= 高点*1.001

        # ===== 2️⃣ 趋势追单判断 =====
        # 判断是否低成交量（趋势启动前缩量）
        low_vol = is_low_volume(df_1m)
        # 判断是否成交量放量（趋势启动信号）
        vol_expand = is_volume_expand(df_1m)

        # 多周期多头趋势一致
        trend_long = (
                trend_5m in ["🟢 强多", "🟡 偏多"]
                and trend_15m in ["🟢 强多", "🟡 偏多"]
                and trend_1h in ["🟢 强多", "🟡 偏多"]
        )

        # 多周期空头趋势一致
        trend_short = (
                trend_5m in ["🔴 强空", "🟠 偏空"]
                and trend_15m in ["🔴 强空", "🟠 偏空"]
                and trend_1h in ["🔴 强空", "🟠 偏空"]
        )

        # ===== 金叉 / 死叉（趋势启动确认）
        golden_cross = is_macd_golden_cross(df_5m)  # 5分钟MACD金叉
        dead_cross = is_macd_dead_cross(df_5m)  # 5分钟MACD死叉

        trend_signal = False  # 趋势信号初始化

        # 多头趋势追单条件：多周期多头 + 金叉 + 缩量 + 放量
        if trend_long and golden_cross and low_vol and vol_expand:
            trend_signal = "🔵 追多"

        # 空头趋势追单条件：多周期空头 + 死叉 + 缩量 + 放量
        elif trend_short and dead_cross and low_vol and vol_expand:
            trend_signal = "🔴 追空"

        # 重复判断（代码冗余，保留原逻辑）
        if trend_long and golden_cross and low_vol and vol_expand:
            trend_signal = "🔵 做多（趋势启动）"

        elif trend_short and dead_cross and low_vol and vol_expand:
            trend_signal = "🔴 做空（趋势启动）"

        # ===== 3️⃣ 最终信号 =====
        entry_signal = "⛔ 不交易"  # 默认不交易

        # 向上剃头皮：震荡区间 + 靠近低点
        if scalp_signal and near_low:
            entry_signal = "🟢 向上剃头皮"

        # 向下剃头皮：震荡区间 + 靠近高点
        elif scalp_signal and near_high:
            entry_signal = "🟠 向下剃头皮"

        # 趋势追单信号
        elif trend_signal:
            entry_signal = trend_signal

        # ===== 4️⃣ 不追高过滤 =====
        # 计算20周期EMA（判断价格是否偏离均线过远）
        ema20 = df_1m["close"].ewm(span=20).mean().iloc[-1]

        # 价格偏离EMA20超过0.2%，禁止追单
        if abs(price - ema20) / price > 0.002:
            entry_signal = "⛔ 偏离过大，不追"

        # ===== ATR总风控（最终拦截）
        if "禁止交易" in trade_status:
            entry_signal = "⛔ ATR风控：禁止交易"


        # ===============================
        # ✅ 调用统一下单函数
        # ===============================

        order_status = "未下单"  # 初始化下单标记

        # 实盘/测试盘统一下单
        if entry_signal in ["追多", "向上剃头皮"]:
            success = place_order(
                symbol=SYMBOL,
                side="BUY",
                quantity=DEFAULT_ORDER_QTY,
                order_type="SCALPING" if "剃头皮" in entry_signal else "NORMAL",
                scalping_range=(scalp_low, scalp_high) if "剃头皮" in entry_signal else None,
                stop_loss=stop_loss_value,
                take_profit=take_profit_value
            )
            if success:
                order_status = "已下多单"

        elif entry_signal in ["追空", "向下剃头皮"]:
            success = place_order(
                symbol=SYMBOL,
                side="SELL",
                quantity=DEFAULT_ORDER_QTY,
                order_type="SCALPING" if "剃头皮" in entry_signal else "NORMAL",
                scalping_range=(scalp_low, scalp_high) if "剃头皮" in entry_signal else None,
                stop_loss=stop_loss_value,
                take_profit=take_profit_value
            )
            if success:
                order_status = "已下空单"

        # ===============================
        # 打印策略信号与下单状态
        # ===============================
        print("\n====== 策略信号 ======")
    #   print("信号:", entry_signal)
        print("下单状态:", order_status)

        # ===============================
        # 当前交易策略（提取方向）
        # ===============================
        strategy_name = entry_signal  # 简化处理
        print("当前交易策略:", strategy_name, "| 下单状态:", order_status)


        # 打印多周期趋势分析
        print("\n====== 多周期趋势 ======")
        print("5分钟趋势:", trend_5m)
        print("15分钟趋势:", trend_15m)
        print("1小时趋势:", trend_1h)

        # ===============================
        # 周期一致性判断
        # ===============================
        # 三个周期趋势一致，判定为趋势确认
        if trend_5m == trend_15m == trend_1h:
            trend_sync = "✅ 一致"
        else:
            trend_sync = "❌ 不一致"

        print("周期一致:", trend_sync)

        # 打印ATR波动过滤结果
        print("\n====== ATR波动过滤 ======")
        print(atr_1m)
        print(atr_5m)
        print("最终交易状态:", trade_status)

        # 打印策略信号
        print("\n====== 策略信号 ======")
        print("信号:", entry_signal)

        # ===============================
        # 当前交易策略（提取方向）
        # ===============================
        # 策略名称初始化（冗余逻辑，保留原代码）
        if "禁止交易" in trade_status:
            strategy_name = "🚫 禁止交易（ATR风控）"

        elif "偏离过大" in entry_signal:
            strategy_name = "⚠️ 等待回调（不追高）"

        elif "追多" in entry_signal:
            strategy_name = "🔵 追多"

        elif "追空" in entry_signal:
            strategy_name = "🔴 追空"

        elif "向上剃头皮" in entry_signal:
            strategy_name = "🟢 向上剃头皮"

        elif "向下剃头皮" in entry_signal:
            strategy_name = "🟠 向下剃头皮"

        else:
            strategy_name = "⏳ 无交易机会"

        # 简化策略名称（覆盖上面的判断）
        if "追多" in entry_signal:
            strategy_name = "追多"
        elif "追空" in entry_signal:
            strategy_name = "追空"
        elif "向上剃头皮" in entry_signal:
            strategy_name = "向上剃头皮"
        elif "向下剃头皮" in entry_signal:
            strategy_name = "向下剃头皮"

        # 打印最终策略名称
        print("当前交易策略:", strategy_name)
        # 普通策略消息，节流播报
        speak(f"当前交易策略：{strategy_name}", key="strategy_signal", force=False)
        print("当前下单状态:", order_status)
        # 下单消息优先播报，force=True
        speak(f"当前下单状态：{order_status}", key="order_status", force=True)
"""
        # 如果有有效指标，打印指标值
        if indicators:
            print("ATR:", round(indicators["atr"], 4))
            print("MACD:", round(indicators["macd"], 4))
            print("KDJ:", round(indicators["k"], 2),
                  round(indicators["d"], 2),
                  round(indicators["j"], 2))

"""
# ===============================
# 主程序入口
# ===============================
def main():
    print("🚀 系统启动")

    init_klines()  # 初始化历史K线数据
    start_ws()  # 启动WebSocket实时数据
    monitor()  # 启动监控和策略分析
    check_stop_orders()  # 自动检查所有订单的止盈/止损


# 程序入口：只有直接运行时才执行main函数
if __name__ == "__main__":
    main()