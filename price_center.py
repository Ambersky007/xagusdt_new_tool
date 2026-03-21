# ===============================
# price_center.py
# 全局价格中心（统一价格源🔥）
# ===============================

import time
import threading
import requests

# ===============================
# 配置
# ===============================
SYMBOL = "XAGUSDT"
HTTP_BASE = "https://fapi.binance.com"

# WS数据共享（由 main_engine 注入）
ws_price = None
last_ws_ts = 0

# 超时配置
WS_TIMEOUT = 10  # 超过10秒没更新 → 走HTTP

# 锁（线程安全）
lock = threading.Lock()


# ===============================
# 外部注入WS数据（关键设计🔥）
# ===============================
def update_ws_price(price):
    global ws_price, last_ws_ts
    with lock:
        ws_price = price
        last_ws_ts = time.time()


# ===============================
# HTTP获取价格（兜底）
# ===============================
def get_http_price():
    try:
        url = f"{HTTP_BASE}/fapi/v1/ticker/price"
        r = requests.get(url, params={"symbol": SYMBOL}, timeout=3)
        return float(r.json()["price"])
    except Exception as e:
        print(f"⚠️ HTTP价格获取失败: {e}")
        return None


# ===============================
# 对外统一接口（核心🔥）
# ===============================
def get_price():
    """
    优先WS价格，超时自动切HTTP
    """
    global ws_price, last_ws_ts

    with lock:
        current_ws_price = ws_price
        ts = last_ws_ts

    # WS有效
    if current_ws_price and (time.time() - ts < WS_TIMEOUT):
        return current_ws_price

    # WS失效 → HTTP兜底
    http_price = get_http_price()
    if http_price:
        return http_price

    # 全挂 → 返回None
    return None