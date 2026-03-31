import threading
import time
import pandas as pd
import yfinance as yf
import logging
logging.getLogger().setLevel(logging.CRITICAL)

# ===========================
# 全局状态（线程安全 + 完整状态追踪）
# ===========================
lock = threading.Lock()

prices = {}
dxy_history = []

current_dxy_val = None          # 原始DXY
current_dxy_smooth = None       # 平滑DXY（给策略用）
current_dxy_trend = None

last_update_time = 0            # 最后一次有效更新时间（关键）

MAX_LEN = 200

required_symbols = [
    "EURUSD=X", "JPY=X", "GBPUSD=X",
    "CAD=X", "SEK=X", "CHF=X"
]

# ===========================
# 参数（可调）
# ===========================
EMA_SHORT = 5
EMA_LONG = 20
NOISE_THRESHOLD = 0.02   # 小于0.02%认为无效波动（抗抖）
DATA_FRESH_SEC = 3      # 数据最大允许延迟（秒）→ 新鲜度判断

# ===========================
# 更新价格（线程安全）
# ===========================
def update_price(symbol, price):
    with lock:
        prices[symbol] = price

# ===========================
# 计算DXY（必须6个货币全齐）
# ===========================
def calculate_dxy():
    with lock:
        # 强校验：6个货币必须全部存在
        if not all(k in prices for k in required_symbols):
            return None

        eurusd = prices['EURUSD=X']
        usdjpy = prices['JPY=X']
        gbpusd = prices['GBPUSD=X']
        usdcad = prices['CAD=X']
        usdsek = prices['SEK=X']
        usdchf = prices['CHF=X']

    try:
        dxy = 50.14348112 * (
            (eurusd ** -0.576) *
            (usdjpy ** 0.136) *
            (gbpusd ** -0.119) *
            (usdcad ** 0.091) *
            (usdsek ** 0.042) *
            (usdchf ** 0.036)
        )
        return dxy
    except Exception as e:
        print(f"❌ DXY计算错误: {e}")
        return None

# ===========================
# 更新趋势 + 平滑（核心）
# ===========================
def update_dxy(dxy_value):
    global dxy_history

    dxy_history.append(dxy_value)

    if len(dxy_history) > MAX_LEN:
        dxy_history = dxy_history[-MAX_LEN:]

    if len(dxy_history) < 5:
        return None, "FLAT"

    s = pd.Series(dxy_history)

    ema_short = s.ewm(span=EMA_SHORT).mean().iloc[-1]
    ema_long = s.ewm(span=EMA_LONG).mean().iloc[-1]

    # 抗抖动
    prev = s.iloc[-2]
    change_pct = abs((dxy_value - prev) / prev * 100)

    if change_pct < NOISE_THRESHOLD:
        return ema_short, "FLAT"

    # 趋势判断
    diff = ema_short - ema_long
    if diff > 0:
        strength = "STRONG" if abs(diff) > 0.05 else "WEAK"
        trend = f"{strength}_UP"
    elif diff < 0:
        strength = "STRONG" if abs(diff) > 0.05 else "WEAK"
        trend = f"{strength}_DOWN"
    else:
        trend = "FLAT"

    return ema_short, trend

# ===========================
# WS回调（核心：每次更新都刷新时间戳）
# ===========================
def on_message(msg):
    global current_dxy_val, current_dxy_trend, current_dxy_smooth, last_update_time

    try:
        symbol = msg.get('id')
        price = msg.get('price')

        if symbol in required_symbols and price:
            update_price(symbol, float(price))

        dxy = calculate_dxy()

        if dxy:
            smooth, trend = update_dxy(dxy)

            with lock:
                current_dxy_val = dxy
                current_dxy_smooth = smooth
                current_dxy_trend = trend
                last_update_time = time.time()  # ✅ 每次成功计算都更新时间

    except Exception as e:
        print(f"❌ WS错误: {e}")

# ===========================
# 启动线程（不变）
# ===========================
def start_dxy_engine():
    def run():
        print("🚀 DXY Engine 启动（实盘安全版）")
        ws = yf.WebSocket()
        ws.subscribe(required_symbols)
        ws.on_message = on_message
        ws.listen()

    t = threading.Thread(target=run, daemon=True)
    t.start()

# ===========================
# 对外接口（全部优化：非阻塞 + 新鲜度）
# ===========================
def get_dxy():
    """默认返回平滑值"""
    with lock:
        return current_dxy_smooth

def get_raw_dxy():
    """返回原始DXY"""
    with lock:
        return current_dxy_val

def get_dxy_trend():
    """返回趋势"""
    with lock:
        return current_dxy_trend

def get_dxy_age():
    """返回数据延迟秒数"""
    with lock:
        return time.time() - last_update_time

# ✅ 核心修复：非阻塞 + 新鲜度检查（实盘标准）
def is_dxy_ready(max_delay=DATA_FRESH_SEC):
    """
    非阻塞检查：
    1. 有值
    2. 数据是新鲜的（最近max_delay秒内更新）
    """
    with lock:
        if current_dxy_smooth is None:
            return False
        if last_update_time <= 0:
            return False
        return (time.time() - last_update_time) < max_delay

# ✅ 专业级：状态调试接口（监控神器）
def get_dxy_status():
    with lock:
        delay = time.time() - last_update_time if last_update_time > 0 else None
        return {
            "has_smooth": current_dxy_smooth is not None,
            "raw_value": round(current_dxy_val, 3) if current_dxy_val else None,
            "trend": current_dxy_trend,
            "last_update_ts": last_update_time,
            "delay_sec": round(delay, 2) if delay else None,
            "is_fresh": (delay < DATA_FRESH_SEC) if delay else False
        }