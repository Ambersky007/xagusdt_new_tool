import requests
import websocket
import json
import threading
import time
import pandas as pd
import numpy as np
from collections import deque

# ===============================
# 配置
# ===============================
SYMBOL = "XAGUSDT"
WS_SYMBOL = SYMBOL.lower()

HTTP_BASE = "https://fapi.binance.com"

# ✅ 多流（官方标准）
WS_URL = f"wss://fstream.binance.com/stream?streams={WS_SYMBOL}@aggTrade/{WS_SYMBOL}@markPrice/{WS_SYMBOL}@depth20/{WS_SYMBOL}@kline_1m"

INTERVALS = ["1m", "5m", "15m", "30m", "1h"]
INIT_LIMIT = 120

# ===============================
# 数据缓存
# ===============================
cache = {
    "price": None,
    "depth": None,
    "klines": {i: deque(maxlen=200) for i in INTERVALS},
}

lock = threading.Lock()
ws_alive = False


# ===============================
# 初始化K线（120根）
# ===============================
def init_klines():
    print("📥 初始化K线...")

    for interval in INTERVALS:
        try:
            url = f"{HTTP_BASE}/fapi/v1/klines"
            params = {"symbol": SYMBOL, "interval": interval, "limit": INIT_LIMIT}

            data = requests.get(url, params=params, timeout=10).json()

            with lock:
                cache["klines"][interval].clear()
                for k in data:
                    cache["klines"][interval].append({
                        "time": k[0],
                        "open": float(k[1]),
                        "high": float(k[2]),
                        "low": float(k[3]),
                        "close": float(k[4]),
                        "volume": float(k[5])
                    })

            print(f"✅ {interval} OK")

        except Exception as e:
            print(f"❌ {interval} 初始化失败:", e)


# ===============================
# HTTP兜底更新
# ===============================
def http_update():
    url = f"{HTTP_BASE}/fapi/v1/klines"

    for interval in INTERVALS:
        try:
            last_time = cache["klines"][interval][-1]["time"]

            params = {"symbol": SYMBOL, "interval": interval, "limit": 2}
            data = requests.get(url, params=params, timeout=5).json()

            new_k = data[-1]

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

        except:
            pass

    # ✅ 价格兜底
    try:
        r = requests.get(f"{HTTP_BASE}/fapi/v1/ticker/price",
                         params={"symbol": SYMBOL}, timeout=3)
        with lock:
            cache["price"] = float(r.json()["price"])
    except:
        pass


# ===============================
# 技术指标
# ===============================
def calc_indicators(df):
    if len(df) < 30:
        return None

    close = df["close"]

    ema12 = close.ewm(span=12).mean()
    ema26 = close.ewm(span=26).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9).mean()

    low_min = df["low"].rolling(9).min()
    high_max = df["high"].rolling(9).max()
    rsv = (close - low_min) / (high_max - low_min + 1e-9) * 100

    k = rsv.ewm(com=2).mean()
    d = k.ewm(com=2).mean()
    j = 3 * k - 2 * d

    atr = (df["high"] - df["low"]).rolling(14).mean()

    return {
        "macd": macd.iloc[-1],
        "signal": signal.iloc[-1],
        "k": k.iloc[-1],
        "d": d.iloc[-1],
        "j": j.iloc[-1],
        "atr": atr.iloc[-1]
    }


# ===============================
# 趋势分析（多周期🔥）
# ===============================
def analyze_trend(df):
    if len(df) < 30:
        return "数据不足"

    close = df["close"]

    ema12 = close.ewm(span=12).mean()
    ema26 = close.ewm(span=26).mean()

    macd = ema12 - ema26
    signal = macd.ewm(span=9).mean()

    latest_close = close.iloc[-1]
    latest_ema12 = ema12.iloc[-1]
    latest_ema26 = ema26.iloc[-1]
    latest_macd = macd.iloc[-1]
    latest_signal = signal.iloc[-1]

    score = 0

    # EMA趋势
    if latest_ema12 > latest_ema26:
        score += 1
    else:
        score -= 1

    # MACD趋势
    if latest_macd > latest_signal:
        score += 1
    else:
        score -= 1

    # 价格位置
    if latest_close > latest_ema12:
        score += 1
    else:
        score -= 1

    # 结果判断
    if score >= 3:
        return "🟢 强多"
    elif score == 2:
        return "🟡 偏多"
    elif score <= -3:
        return "🔴 强空"
    elif score == -2:
        return "🟠 偏空"
    else:
        return "⚪ 震荡"

# ===============================
# WebSocket
# ===============================
def on_message(ws, message):
    global ws_alive
    ws_alive = True

    msg = json.loads(message)

    # ✅ 关键修复：解析combined stream
    data = msg.get("data", msg)

    with lock:
        # ===============================
        # 成交价（主）
        # ===============================
        if data.get("e") == "aggTrade":
            cache["price"] = float(data["p"])

        # ===============================
        # 标记价格（备用）
        # ===============================
        elif data.get("e") == "markPriceUpdate":
            if cache["price"] is None:
                cache["price"] = float(data["p"])

        # ===============================
        # 深度
        # ===============================
        elif data.get("e") == "depthUpdate":
            cache["depth"] = data

        # ===============================
        # K线
        # ===============================
        elif data.get("e") == "kline":
            k = data["k"]

            new_k = {
                "time": k["t"],
                "open": float(k["o"]),
                "high": float(k["h"]),
                "low": float(k["l"]),
                "close": float(k["c"]),
                "volume": float(k["v"])
            }

            if cache["klines"]["1m"][-1]["time"] != new_k["time"]:
                cache["klines"]["1m"].append(new_k)


def on_error(ws, error):
    global ws_alive
    ws_alive = False
    print("❌ WS错误:", error)


def on_close(ws, *args):
    global ws_alive
    ws_alive = False
    print("⚠️ WS关闭")


def start_ws():
    print("🚀 启动WS...")

    ws = websocket.WebSocketApp(
        WS_URL,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close
    )

    threading.Thread(target=ws.run_forever, daemon=True).start()

# ===============================
# ATR波动过滤器（双周期）
# ===============================
def atr_filter(df, name=""):
    if len(df) < 50:
        return f"{name} 数据不足"

    atr = (df["high"] - df["low"]).rolling(14).mean()
    current_atr = atr.iloc[-1]
    atr_mean = atr.rolling(50).mean().iloc[-1]

    if atr_mean == 0 or np.isnan(atr_mean):
        return f"{name} 数据异常"

    if current_atr >= atr_mean * 2:
        return f"{name} ❌ 波动过大"
    elif current_atr <= atr_mean * 0.5:
        return f"{name} ❌ 波动过小"
    else:
        return f"{name} ✅ 波动正常"

# ===============================
# ATR最终交易判断函数
# ===============================
def can_trade(atr_1m_status, atr_5m_status):
    if "过大" in atr_1m_status:
        return "❌ 禁止交易（短期波动过大）"
    if "过小" in atr_5m_status:
        return "❌ 禁止交易（市场太平）"
    return "✅ 可以交易"
# ===============================
# 监控输出
# ===============================
def monitor():
    while True:
        time.sleep(5)

        # WS挂了 → HTTP兜底
        if not ws_alive:
            print("⚠️ WS不可用 → HTTP接管")
            http_update()

        # 价格仍然没有 → 强制HTTP
        if cache["price"] is None:
            http_update()

        with lock:
            df_1m = pd.DataFrame(cache["klines"]["1m"])
            df_5m = pd.DataFrame(cache["klines"]["5m"])
            df_15m = pd.DataFrame(cache["klines"]["15m"])
            df_1h = pd.DataFrame(cache["klines"]["1h"])


        if df_1m.empty:
            continue

        indicators = calc_indicators(df_1m)
        vol = df_1m["volume"]

        # -----------------------------
        # ATR双周期过滤
        # -----------------------------
        atr_1m = atr_filter(df_1m, "1m")
        atr_5m = atr_filter(df_5m, "5m")

        # 最终交易状态
        trade_status = can_trade(atr_1m, atr_5m)

        print("\n====== 实时数据 ======")
        print("价格:", cache["price"])
        print("成交量均值:", round(vol.mean(), 2))
        print("最大成交量:", round(vol.max(), 2))
        print("最小成交量:", round(vol.min(), 2))
        trend_5m = analyze_trend(df_5m)
        trend_15m = analyze_trend(df_15m)
        trend_1h = analyze_trend(df_1h)


        print("\n====== 多周期趋势 ======")
        print("5分钟趋势:", trend_5m)
        print("15分钟趋势:", trend_15m)
        print("1小时趋势:", trend_1h)

        print("\n====== ATR波动过滤 ======")
        print(atr_1m)
        print(atr_5m)
        print("最终交易状态:", trade_status)

        if indicators:
            print("ATR:", round(indicators["atr"], 4))
            print("MACD:", round(indicators["macd"], 4))
            print("KDJ:", round(indicators["k"], 2),
                  round(indicators["d"], 2),
                  round(indicators["j"], 2))


# ===============================
# 主程序
# ===============================
def main():
    print("🚀 系统启动")

    init_klines()
    start_ws()
    monitor()


if __name__ == "__main__":
    main()