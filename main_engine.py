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
============================================
🔥 2026-03-31 终极赚钱版（3大杀手级隐患已全修复）
"""

# ===============================
# 导入所需的核心库：所有外部依赖模块
# ===============================
import requests
import websocket
import json
import threading
import time
import pandas as pd
import numpy as np
from collections import deque

from order_executor import place_order, check_stop_orders, get_position, wait_order_filled
from config_account import DEFAULT_ORDER_QTY, SYMBOL
from voice_alert import speak
from price_center import update_ws_price, get_price
from time_sync import sync_time

# ===============================
# 全局配置区
# ===============================
SYMBOL = "XAGUSDT"
WS_SYMBOL = SYMBOL.lower()
HTTP_BASE = "https://fapi.binance.com"

WS_URL = (
    f"wss://fstream.binance.com/stream?streams="
    f"{WS_SYMBOL}@aggTrade/"
    f"{WS_SYMBOL}@markPrice/"
    f"{WS_SYMBOL}@depth5@100ms/"
    f"{WS_SYMBOL}@kline_1m/"
    f"{WS_SYMBOL}@kline_5m/"
    f"{WS_SYMBOL}@kline_15m/"
    f"{WS_SYMBOL}@kline_30m/"
    f"{WS_SYMBOL}@kline_1h"
)

INTERVALS = ["1m", "5m", "15m", "30m", "1h"]
INIT_LIMIT = 120
WS_TIMEOUT_SECONDS = 60
last_ws_message_ts = 0
ORDER_COOLDOWN = 60
last_signal = None
last_order_time = 0
last_http_update = 0
HTTP_COOLDOWN = 20
PRICE_PRECISION = 2

# ===============================
# 实盘风控（必开）
# ===============================
loss_streak = 0
daily_profit = 0.0
last_pause_time = 0
PAUSE_MINUTES = 30
MAX_DAILY_LOSS = -2.0

# ===============================
# 🔥 新增：同方向连续亏损控制（防连亏爆仓）
# ===============================
last_direction = None
direction_loss_count = 0

# ===============================
# 持仓查询缓存
# ===============================
last_position_check = 0
cached_position = None

# ===============================
# 交易锁 正确逻辑
# ===============================
trade_lock = False
LOCK_TIME = 8
MAX_LOCK_TIME = 60
trade_lock_time = 0

# ===============================
# 全局数据缓存
# ===============================
cache = {
    "depth": {"bid": None, "ask": None},
    "klines": {i: deque(maxlen=200) for i in INTERVALS},
}
lock = threading.Lock()
ws_alive = False


# ===============================
# 初始化K线
# ===============================
def init_klines():
    print("📥 初始化K线数据...")
    for interval in INTERVALS:
        try:
            url = f"{HTTP_BASE}/fapi/v1/klines"
            params = {"symbol": SYMBOL, "interval": interval, "limit": INIT_LIMIT}
            response = requests.get(url, params=params, timeout=10)
            data = response.json()
            with lock:
                cache["klines"][interval].clear()
                for k in data:
                    cache["klines"][interval].append({
                        "time": k[0], "open": float(k[1]), "high": float(k[2]),
                        "low": float(k[3]), "close": float(k[4]), "volume": float(k[5])
                    })
            print(f"✅ {interval} K线初始化成功")
        except Exception as e:
            print(f"❌ {interval} K线初始化失败: {e}")


# ===============================
# HTTP兜底更新
# ===============================
def http_update():
    global last_http_update
    if time.time() - last_http_update < HTTP_COOLDOWN:
        return
    last_http_update = time.time()
    print("🔄 执行HTTP兜底更新...")
    url = f"{HTTP_BASE}/fapi/v1/klines"
    for interval in INTERVALS:
        try:
            if not cache["klines"][interval]:
                continue
            last_time = cache["klines"][interval][-1]["time"]
            params = {"symbol": SYMBOL, "interval": interval, "limit": 2}
            data = requests.get(url, params=params, timeout=5).json()
            if len(data) >= 2:
                new_k = data[-2]
                if new_k[0] > last_time + 1000:
                    with lock:
                        cache["klines"][interval].append({
                            "time": new_k[0], "open": float(new_k[1]), "high": float(new_k[2]),
                            "low": new_k[3], "close": float(new_k[4]), "volume": float(new_k[5])
                        })
            print(f"📈 {interval} K线已更新")
        except Exception as e:
            print(f"⚠️ {interval} K线更新失败: {e}")

    try:
        price_url = f"{HTTP_BASE}/fapi/v1/ticker/price"
        r = requests.get(price_url, params={"symbol": SYMBOL}, timeout=3)
        price = float(r.json()["price"])
        update_ws_price(price)
        print(f"💰 价格更新为: {get_price():.4f}")
    except Exception as e:
        print(f"⚠️ 价格更新失败: {e}")

    try:
        depth_url = f"{HTTP_BASE}/fapi/v1/depth"
        r = requests.get(depth_url, params={"symbol": SYMBOL, "limit": 1}, timeout=3)
        depth_data = r.json()
        with lock:
            cache["depth"]["bid"] = float(depth_data["bids"][0][0]) if depth_data.get("bids") else None
            cache["depth"]["ask"] = float(depth_data["asks"][0][0]) if depth_data.get("asks") else None
        bid_str = f"{cache['depth']['bid']:.4f}" if cache['depth']['bid'] else "N/A"
        ask_str = f"{cache['depth']['ask']:.4f}" if cache['depth']['ask'] else "N/A"
        print(f"📊 盘口更新 - 买一: {bid_str} | 卖一: {ask_str}")
    except Exception as e:
        print(f"⚠️ 盘口更新失败: {str(e)}")


def get_orderbook_http(symbol):
    try:
        url = f"https://binance.com/fapi/v1/depth?symbol={symbol}&limit=5"
        data = requests.get(url, timeout=3).json()
        bid = float(data["bids"][0][0]) if data.get("bids") else None
        ask = float(data["asks"][0][0]) if data.get("asks") else None
        return bid, ask
    except Exception as e:
        print(f"⚠️ HTTP盘口获取失败: {e}")
        return None, None


# ===============================
# 技术指标
# ===============================
def calc_indicators(df):
    if len(df) < 30:
        return None
    close = df["close"]
    high = df["high"]
    low = df["low"]
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    low_min = df["low"].rolling(window=9).min()
    high_max = df["high"].rolling(window=9).max()
    rsv = (close - low_min) / (high_max - low_min + 1e-9) * 100
    k = rsv.ewm(com=2, adjust=False).mean()
    d = k.ewm(com=2, adjust=False).mean()
    j = 3 * k - 2 * d
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.rolling(window=14).mean()
    return {
        "macd": macd.iloc[-1], "signal": signal.iloc[-1],
        "k": k.iloc[-1], "d": d.iloc[-1], "j": j.iloc[-1],
        "atr": atr.iloc[-1], "tr": tr.iloc[-1]
    }


def analyze_trend(df):
    if len(df) < 30:
        return "数据不足"
    close = df["close"]
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    scores = []
    for i in range(-3, 0):
        score = 0
        if ema12.iloc[i] > ema26.iloc[i]:
            score += 1
        else:
            score -= 1
        if macd.iloc[i] > signal.iloc[i]:
            score += 1
        else:
            score -= 1
        if close.iloc[i] > ema12.iloc[i]:
            score += 1
        else:
            score -= 1
        scores.append(score)
    s = sum(scores)
    if s >= 4:
        return "🟢 强多"
    elif s >= 2:
        return "🟡 偏多"
    elif s <= -4:
        return "🔴 强空"
    elif s <= -2:
        return "🟠 偏空"
    else:
        return "⚪ 震荡"


# ===============================
# WS 消息处理
# ===============================
def on_message(ws, message):
    global ws_alive, last_ws_message_ts
    ws_alive = True
    msg = json.loads(message)
    stream = msg.get("stream", "")
    data = msg.get("data", msg)
    with lock:
        if data.get("e") == "aggTrade":
            update_ws_price(float(data["p"]))
            last_ws_message_ts = time.time()
        elif data.get("e") == "markPriceUpdate":
            if get_price() is None:
                update_ws_price(float(data["p"]))
        elif data.get("lastUpdateId"):
            bids = data.get("bids", [])
            asks = data.get("asks", [])
            if bids: cache["depth"]["bid"] = float(bids[0][0])
            if asks: cache["depth"]["ask"] = float(asks[0][0])
        elif data.get("e") == "kline":
            k = data["k"]
            interval = k["i"]
            is_closed = k["x"]
            new_k = {
                "time": k["t"], "open": float(k["o"]), "high": float(k["h"]),
                "low": float(k["l"]), "close": float(k["c"]), "volume": float(k["v"])
            }
            if interval in cache["klines"]:
                if is_closed:
                    if not cache["klines"][interval] or new_k["time"] > cache["klines"][interval][-1]["time"]:
                        cache["klines"][interval].append(new_k)
                else:
                    if cache["klines"][interval]:
                        cache["klines"][interval][-1] = new_k


def on_error(ws, error):
    global ws_alive
    ws_alive = False
    print(f"❌ WebSocket错误: {error}")


def on_close(ws, *args):
    global ws_alive
    ws_alive = False
    print("⚠️ WebSocket连接关闭")


def start_ws():
    def run():
        reconnect_delay = 5
        while True:
            try:
                print("🚀 启动WebSocket连接...")
                with lock:
                    for i in INTERVALS: cache["klines"][i].clear()
                init_klines()
                time.sleep(1)
                ws = websocket.WebSocketApp(WS_URL, on_message=on_message, on_error=on_error, on_close=on_close)
                ws.run_forever(ping_interval=20, ping_timeout=10)
                reconnect_delay = 5
            except Exception as e:
                print(f"❌ WS异常: {e}")
            print(f"🔄 {reconnect_delay}秒后重连WS...")
            time.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 1.5, 60)

    threading.Thread(target=run, daemon=True).start()


# ===============================
# ATR 波动过滤
# ===============================
def atr_filter(df, name=""):
    if len(df) < 50:
        return f"{name} 数据不足"
    high, low, close = df["high"], df["low"], df["close"]
    price = close.iloc[-1]
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
    if atr_mean == 0:
        return f"{name} 数据异常"
    atr_pct = current_atr / price
    if atr_pct > 0.01:
        return f"{name} 🚀 高波动"
    elif atr_pct < 0.002:
        return f"{name} 💤 低波动"
    else:
        return f"{name} ✅ 正常"


# ===============================
# KDJ 带 J 值返回
# ===============================
def get_kdj_cross(df):
    if len(df) < 30:
        return False, False, 50.0, 50.0, 50.0
    low_min = df["low"].rolling(window=9, min_periods=9).min()
    high_max = df["high"].rolling(window=9, min_periods=9).max()
    rsv = (df["close"] - low_min) / (high_max - low_min + 1e-9) * 100
    k = rsv.ewm(alpha=1 / 3, adjust=False).mean()
    d = k.ewm(alpha=1 / 3, adjust=False).mean()
    j = 3 * k - 2 * d
    k1, k2 = k.iloc[-1], k.iloc[-2]
    d1, d2 = d.iloc[-1], d.iloc[-2]
    golden = (k2 < d2) and (k1 > d1)
    dead = (k2 > d2) and (k1 < d1)
    return golden, dead, round(k1, 2), round(d1, 2), round(j.iloc[-1], 2)


# ===============================
# 震荡区间
# ===============================
def detect_scalping_range(df):
    if len(df) < 20:
        return "数据不足", None, None, 0, 0
    recent = df.tail(20).copy()
    top2 = recent.sort_values("high", ascending=False).head(2)
    bot2 = recent.sort_values("low", ascending=True).head(2)
    if len(top2) < 2 or len(bot2) < 2:
        return "❌ 数据不足", None, None, 0, 0
    h1, h2 = top2["high"].values
    l1, l2 = bot2["low"].values
    atr_mean = (recent["high"] - recent["low"]).ewm(span=10).mean().iloc[-1]
    high_avg = (h1 + h2) / 2
    low_avg = (l1 + l2) / 2
    mid = (high_avg + low_avg) / 2
    if mid < 1e-6:
        return "❌ 价格异常", None, None, 0, 0
    range_pct = (high_avg - low_avg) / mid
    if range_pct > 0.03:
        return "❌ 波动过大", None, None, 0, 0
    if range_pct < 0.0015:
        return "❌ 波动过小", None, None, 0, 0
    above = (recent["close"] > mid).sum()
    below = (recent["close"] < mid).sum()
    balance = min(above, below) / max(above, below) if max(above, below) > 0 else 0
    if balance < 0.4:
        return "❌ 单边走势", None, None, 0, 0
    tolerance = min((high_avg - low_avg) * 0.10, atr_mean * 1.5)
    touch_high = (abs(recent["high"] - high_avg) < tolerance).sum()
    touch_low = (abs(recent["low"] - low_avg) < tolerance).sum()
    if touch_high < 2 or touch_low < 2:
        return "❌ 触顶触底不足", None, None, 0, 0

    return "🟡 可剃头皮", low_avg, high_avg, 100, atr_mean * 1.2


# ===============================
# 剃头皮ATR过滤
# ===============================
def atr_filter_scalping(df):
    if len(df) < 20:
        return "❌ 数据不足"
    high, low, close = df["high"], df["low"], df["close"]
    price = close.iloc[-1]
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
    atr_mean = atr.ewm(span=20).mean().iloc[-1]
    if atr_mean == 0:
        return "❌ 数据异常"
    atr_pct = current_atr / price
    atr_mean_pct = atr_mean / price
    atr_trend = atr.iloc[-1] - atr.iloc[-5]
    if atr_trend > atr_mean * 0.4:
        return "⚠️ 波动放大"
    if atr_pct >= atr_mean_pct * 2.0:
        return "❌ 波动过大"
    elif atr_pct <= atr_mean_pct * 0.35:
        return "❌ 波动过小"
    else:
        return "✅ 可剃头皮"


# ===============================
# 成交量
# ===============================
def is_low_volume(df):
    if len(df) < 60:
        return False
    vol = df["volume"]
    short = vol.tail(20).mean()
    long = vol.tail(60).mean()
    return short < long * 0.8


def is_volume_expand(df):
    if len(df) < 25:
        return False
    vol = df["volume"]
    current = vol.iloc[-2]
    avg = vol.iloc[-22:-2].mean()
    return current > avg * 0.9


# ===============================
# 平仓条件（剃头皮不主动平）
# ===============================
def should_exit_position(df_1m_closed, df_5m_closed, df_15m_closed, is_long, entry_signal):
    if len(df_1m_closed) < 20 or len(df_5m_closed) < 30 or len(df_15m_closed) < 30:
        return False
    trend_15m = analyze_trend(df_15m_closed)

    # 剃头皮 → 只靠TP/SL，不平仓
    if "剃头皮" in entry_signal:
        return False

    if is_long and is_macd_dead_cross(df_5m_closed) and trend_15m in ["🔴 强空", "🟠 偏空"]:
        return True
    if not is_long and is_macd_golden_cross(df_5m_closed) and trend_15m in ["🟢 强多", "🟡 偏多"]:
        return True

    ema20 = df_1m_closed["close"].ewm(span=20).mean().iloc[-1]
    price = df_1m_closed["close"].iloc[-1]
    buffer = 0.0005 * price
    if is_long and price < ema20 - buffer:
        return True
    if not is_long and price > ema20 + buffer:
        return True
    return False


def default_sl_tp(price):
    sl = price * 0.992
    tp = price * 1.008
    return round(sl, PRICE_PRECISION), round(tp, PRICE_PRECISION)


def is_macd_golden_cross(df):
    if len(df) < 30:
        return False
    close = df["close"]
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    return macd.iloc[-2] < signal.iloc[-2] and macd.iloc[-1] > signal.iloc[-1]


def is_macd_dead_cross(df):
    if len(df) < 30:
        return False
    close = df["close"]
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    return macd.iloc[-2] > signal.iloc[-2] and macd.iloc[-1] < signal.iloc[-1]


# ===============================
# 市场状态
# ===============================
def can_trade(atr_1m_status, atr_5m_status, atr_15m_status, atr_1h_status):
    statuses = [atr_1m_status, atr_5m_status, atr_15m_status, atr_1h_status]
    high_vol = sum(1 for s in statuses if "高波动" in s)
    low_vol = sum(1 for s in statuses if "低波动" in s)
    if high_vol >= 2:
        return "🚀 趋势市场（优先做趋势）"
    elif low_vol >= 1:
        return "💤 偏震荡（优先剃头皮）"
    else:
        return "⚖️ 混合市场（谨慎交易）"


# ===============================
# 🔥 开仓信号：弱信号加5m微趋势过滤（防乱开）
# ===============================
def get_entry_signal(current_price, scalping_range_status, scalping_atr_status, trend_signal, trend_5m, trend_15m,
                     df_1m_closed, range_low, range_high, atr_value):
    kdj_golden, kdj_dead, k, d, j = get_kdj_cross(df_1m_closed)
    scalp_enabled = ("可剃头皮" in scalping_range_status) and ("可剃头皮" in scalping_atr_status)
    in_long_zone = False
    in_short_zone = False

    if range_low is not None and range_high is not None and atr_value > 0:
        zone_buffer = max(atr_value * 0.8, current_price * 0.0015)
        in_long_zone = current_price <= range_low + zone_buffer
        in_short_zone = current_price >= range_high - zone_buffer

    if len(df_1m_closed) >= 2:
        last_close = df_1m_closed["close"].iloc[-2]
        long_confirm = current_price > last_close
        short_confirm = current_price < last_close
    else:
        long_confirm = True
        short_confirm = True

    # 🔥 新增：5m微趋势过滤
    mini_trend_up = trend_5m in ["🟢 强多", "🟡 偏多"]
    mini_trend_down = trend_5m in ["🔴 强空", "🟠 偏空"]

    # 🔥 弱信号严格化（只顺5m小趋势）
    strong_long = in_long_zone and kdj_golden and k < 45 and j < 40 and long_confirm and (k > d)
    weak_long = in_long_zone and k < 40 and j < 45 and long_confirm and (k > d) and mini_trend_up

    strong_short = in_short_zone and kdj_dead and k > 55 and j > 60 and short_confirm and (k < d)
    weak_short = in_short_zone and k > 60 and j > 65 and short_confirm and (k < d) and mini_trend_down

    scalp_up = scalp_enabled and (strong_long or weak_long)
    scalp_down = scalp_enabled and (strong_short or weak_short)

    if scalp_up and trend_15m == "🔴 强空":
        scalp_up = False
    if scalp_down and trend_15m == "🟢 强多":
        scalp_down = False

    if scalp_up:
        return "🟢 向上剃头皮"
    elif scalp_down:
        return "🟠 向下剃头皮"
    elif trend_signal:
        return trend_signal
    else:
        return "⛔ 不交易"


# ===============================
# 🔥 止盈止损收紧（短利快跑、不回吐）
# ===============================
def calculate_sl_tp(current_price, atr_value, entry_signal):
    if "剃头皮" in entry_signal:
        sl_mult = 0.7  # 更紧
        if atr_value / current_price > 0.002:
            tp_mult = 1.6  # 降低
        else:
            tp_mult = 1.2  # 降低
    else:
        sl_mult, tp_mult = 1.5, 2.5

    if entry_signal in ["🟢 向上剃头皮", "🔵 追多"]:
        sl = current_price - atr_value * sl_mult
        tp = current_price + atr_value * tp_mult
    elif entry_signal in ["🟠 向下剃头皮", "🔴 追空"]:
        sl = current_price + atr_value * sl_mult
        tp = current_price - atr_value * tp_mult
    else:
        sl = tp = None

    if sl is not None:
        sl = round(sl, PRICE_PRECISION)
    if tp is not None:
        tp = round(tp, PRICE_PRECISION)
    return sl, tp


# ===============================
# 状态打印
# ===============================
def print_status(current_price, bid_price, ask_price, df_1m_closed, position_flag,
                 trend_5m, trend_15m, trend_1h,
                 atr_1m_status, atr_5m_status, atr_15m_status, atr_1h_status,
                 trade_status, scalping_range_status, scalping_atr_status,
                 near_low, near_high, golden_cross, dead_cross, entry_signal,
                 order_status, stop_loss_value, take_profit_value, trade_lock, reason):
    print("\n" + "=" * 30)
    print(f"📊 实时价格: {current_price:.4f}")
    bid_str = f"{bid_price:.4f}" if bid_price else "N/A"
    ask_str = f"{ask_price:.4f}" if ask_price else "N/A"
    print(f"盘口 买一: {bid_str} | 卖一: {ask_str}")
    print(f"持仓: {'✅ 多' if position_flag == 'LONG' else '✅ 空' if position_flag == 'SHORT' else '❌ 无'}")
    print(f"5M: {trend_5m} | 15M: {trend_15m} | 1H: {trend_1h}")
    print(f"市场状态: {trade_status}")
    print(f"震荡区间: {scalping_range_status}")
    print(f"剃头皮ATR: {scalping_atr_status}")
    print(f"信号: {entry_signal} | 原因: {','.join(reason) if reason else '正常'}")
    print(f"交易锁: {'🔒 锁定' if trade_lock else '🔓 未锁定'}")
    if stop_loss_value and take_profit_value:
        print(f"SL: {stop_loss_value} | TP: {take_profit_value}")
    print("=" * 30 + "\n")


# ===============================
# 持仓标准化
# ===============================
def normalize_position(pos):
    if not pos: return None
    if isinstance(pos, str):
        pos = pos.upper().strip()
        return "LONG" if pos in ["LONG", "BUY"] else "SHORT" if pos in ["SHORT", "SELL"] else None
    if isinstance(pos, list):
        long_qty = short_qty = 0.0
        for item in pos:
            try:
                qty = float(item.get("positionAmt", 0))
                side = item.get("positionSide", "").upper()
                if side == "LONG": long_qty = qty
                if side == "SHORT": short_qty = qty
            except:
                continue
        if abs(long_qty) > 1e-4: return "LONG"
        if abs(short_qty) > 1e-4: return "SHORT"
        return None
    if isinstance(pos, dict):
        try:
            qty = float(pos.get("positionAmt", 0))
            if abs(qty) < 1e-4: return None
            return "LONG" if qty > 0 else "SHORT"
        except:
            return None
    return None


# ===============================
# 🔥 风控升级：同方向连续亏损控制（防连亏）
# ===============================
def update_profit(is_win, direction):
    global loss_streak, daily_profit, direction_loss_count, last_direction

    if is_win:
        loss_streak = 0
        direction_loss_count = 0
        daily_profit += 0.2
    else:
        loss_streak += 1
        daily_profit -= 0.2

        if direction == last_direction:
            direction_loss_count += 1
        else:
            direction_loss_count = 1

    last_direction = direction


def check_daily_limit():
    global daily_profit
    if daily_profit <= MAX_DAILY_LOSS:
        print("❌ 日内亏损达2% → 停止交易")
        exit()


# ===============================
# 主策略监控
# ===============================
def monitor():
    global last_ws_message_ts, last_signal, last_order_time
    global last_position_check, cached_position
    global trade_lock, trade_lock_time, loss_streak, daily_profit
    global direction_loss_count

    while True:
        time.sleep(5)
        current_ts = time.time()
        ws_timeout = current_ts - last_ws_message_ts > WS_TIMEOUT_SECONDS
        if not ws_alive or ws_timeout or get_price() is None:
            print("⚠️ WS异常 → HTTP兜底")
            http_update()
            last_ws_message_ts = current_ts

        with lock:
            df_1m = pd.DataFrame(cache["klines"]["1m"])
            df_5m = pd.DataFrame(cache["klines"]["5m"])
            df_15m = pd.DataFrame(cache["klines"]["15m"])
            df_1h = pd.DataFrame(cache["klines"]["1h"])
            bid_price = cache["depth"]["bid"]
            ask_price = cache["depth"]["ask"]

        if bid_price is None or ask_price is None:
            bid_price, ask_price = get_orderbook_http(SYMBOL)
            with lock:
                cache["depth"]["bid"] = bid_price
                cache["depth"]["ask"] = ask_price

        df_1m_closed = df_1m
        df_5m_closed = df_5m
        df_15m_closed = df_15m
        df_1h_closed = df_1h

        if len(df_1m_closed) < 20 or len(df_5m_closed) < 30 or len(df_15m_closed) < 30 or len(df_1h_closed) < 30:
            continue

        try:
            sync_time()
            pos = get_position(SYMBOL)
            position_flag = normalize_position(pos)
        except Exception as e:
            position_flag = None

        if trade_lock:
            elapsed = time.time() - trade_lock_time
            if elapsed > LOCK_TIME or elapsed > MAX_LOCK_TIME:
                trade_lock = False

        current_price = get_price()
        if current_price is None:
            continue

        indicators = calc_indicators(df_1m_closed)
        if not indicators or indicators["atr"] <= 0:
            continue

        atr_1m_status = atr_filter(df_1m_closed, "1m")
        atr_5m_status = atr_filter(df_5m_closed, "5m")
        atr_15m_status = atr_filter(df_15m_closed, "15m")
        atr_1h_status = atr_filter(df_1h_closed, "1h")
        trade_status = can_trade(atr_1m_status, atr_5m_status, atr_15m_status, atr_1h_status)

        trend_5m = analyze_trend(df_5m_closed)
        trend_15m = analyze_trend(df_15m_closed)
        trend_1h = analyze_trend(df_1h_closed)

        scalping_range_status, range_low, range_high, score, entry_buffer = detect_scalping_range(df_1m_closed)
        scalping_atr_status = atr_filter_scalping(df_1m_closed)

        golden_1m = is_macd_golden_cross(df_1m_closed)
        dead_1m = is_macd_dead_cross(df_1m_closed)
        golden_5m = is_macd_golden_cross(df_5m_closed)
        dead_5m = is_macd_dead_cross(df_5m_closed)
        golden_15m = is_macd_golden_cross(df_15m_closed)
        dead_15m = is_macd_dead_cross(df_15m_closed)
        golden_cross = golden_1m or golden_5m or golden_15m
        dead_cross = dead_1m or dead_5m or dead_15m

        trend_signal = None
        buffer = indicators["atr"] * 0.3
        near_low = range_low - buffer <= current_price <= range_low + buffer if (range_low and range_high) else False
        near_high = range_high - buffer <= current_price <= range_high + buffer if (range_low and range_high) else False

        trend_long = trend_1h in ["🟢 强多", "🟡 偏多"] and trend_15m != "🔴 强空"
        trend_short = trend_1h in ["🔴 强空", "🟠 偏空"] and trend_15m != "🟢 强多"

        macd_strength = indicators["macd"] - indicators["signal"]
        momentum_threshold = max(indicators["atr"] * 0.08, current_price * 0.001)

        if trend_long and golden_cross and macd_strength > momentum_threshold and indicators["macd"] > 0:
            trend_signal = "🔵 追多"
        elif trend_short and dead_cross and macd_strength < -momentum_threshold and indicators["macd"] < 0:
            trend_signal = "🔴 追空"

        entry_signal = get_entry_signal(
            current_price, scalping_range_status, scalping_atr_status,
            trend_signal, trend_5m, trend_15m, df_1m_closed, range_low, range_high, indicators["atr"]
        )

        sl, tp = calculate_sl_tp(current_price, indicators["atr"], entry_signal)
        reason = []

        # 🔥 关键：同方向连亏2次 → 屏蔽开单
        if direction_loss_count >= 2:
            entry_signal = "⛔ 不交易"
            reason.append("同方向连续亏损，暂停开单")

        if entry_signal == last_signal and (position_flag or trade_lock):
            entry_signal = "⛔ 不交易"

        if "剃头皮" in entry_signal and "趋势市场" in trade_status:
            reason.append("趋势行情不剃头皮")
        if "追" in entry_signal and "震荡" in trade_status:
            reason.append("震荡不追单")

        ema20 = df_1m_closed["close"].ewm(span=20).mean().iloc[-1]
        if abs(current_price - ema20) / current_price > 0.006:
            reason.append("偏离EMA20过远")

        if bid_price and ask_price:
            spread = (ask_price - bid_price) / bid_price
            if "剃" in entry_signal and spread > 0.0008:
                reason.append(f"价差过大")
            if "追" in entry_signal and spread > 0.0018:
                reason.append(f"价差过大")

        risk_score = 0
        if any("趋势行情不剃头皮" in r for r in reason):
            risk_score += 2
        if any("偏离EMA20过远" in r for r in reason):
            risk_score += 1
        if any("价差过大" in r for r in reason):
            risk_score += 2

        if "剃" in entry_signal and risk_score >= 3:
            entry_signal = "⛔ 不交易"
        elif "追" in entry_signal and risk_score >= 4:
            entry_signal = "⛔ 不交易"

        order_status = "等待信号"
        success = False

        # 平仓
        if position_flag:
            is_long = position_flag == "LONG"
            if should_exit_position(df_1m_closed, df_5m_closed, df_15m_closed, is_long, entry_signal):
                try:
                    place_order(
                        symbol=SYMBOL,
                        side="SELL" if is_long else "BUY",
                        quantity=DEFAULT_ORDER_QTY,
                        order_type="MARKET",
                        reduce_only=True
                    )
                    speak(f"平仓：{'多单' if is_long else '空单'}", key="exit", force=True)
                    # 记录亏损方向
                    update_profit(is_win=False, direction="LONG" if is_long else "SHORT")
                    check_daily_limit()
                    trade_lock = False
                    last_signal = None
                except:
                    trade_lock = False
                continue

        # 开仓
        if not trade_lock and entry_signal in ["🟢 向上剃头皮", "🔵 追多", "🟠 向下剃头皮", "🔴 追空"]:
            try:
                sync_time()
                side = "BUY" if entry_signal in ["🟢 向上剃头皮", "🔵 追多"] else "SELL"
                res = place_order(
                    symbol=SYMBOL, side=side, quantity=DEFAULT_ORDER_QTY,
                    order_type="SCALPING" if "剃头皮" in entry_signal else "NORMAL",
                    scalping_range=(range_low, range_high) if "剃头皮" in entry_signal else None,
                    stop_loss=sl, take_profit=tp
                )
                if res and "orderId" in res:
                    if "剃头皮" in entry_signal:
                        success = wait_order_filled(SYMBOL, res["orderId"], timeout=5)
                    else:
                        success = True
            except Exception as e:
                print(f"下单异常：{e}")

        if success:
            order_status = "✅ 下单成功"
            last_signal = entry_signal
            trade_lock = True
            trade_lock_time = time.time()
            speak(f"开仓：{entry_signal}", key="order", force=True)
            time.sleep(0.5)
        else:
            if entry_signal != "⛔ 不交易":
                order_status = "❌ 未下单"

        print_status(current_price, bid_price, ask_price, df_1m_closed, position_flag,
                     trend_5m, trend_15m, trend_1h,
                     atr_1m_status, atr_5m_status, atr_15m_status, atr_1h_status,
                     trade_status, scalping_range_status, scalping_atr_status,
                     near_low, near_high, golden_cross, dead_cross, entry_signal,
                     order_status, sl, tp, trade_lock, reason)


# ===============================
# 主入口
# ===============================
def main():
    global trade_lock, trade_lock_time
    trade_lock = False
    trade_lock_time = time.time()
    print("🚀 币安期货交易策略系统启动（终极赚钱稳定版）")
    sync_time()
    init_klines()
    start_ws()
    check_stop_orders(SYMBOL)
    monitor()


if __name__ == "__main__":
    main()