# ===============================
# market_data.py
# 行情数据模块（320系统数据层🔥）
# 职责：只负责“获取 + 缓存市场数据”，不做任何策略判断
# 核心能力：
# 1. 多周期K线初始化与实时更新
# 2. WebSocket实时行情接收（成交、标记价、盘口、1分钟K线）
# 3. HTTP兜底更新（WebSocket断开时保障数据可用）
# 4. 线程安全的数据缓存（防止多线程冲突）
# ===============================

# ===============================
# 导入核心依赖库
# ===============================
import requests       # HTTP请求库：用于调用币安REST API获取历史K线、兜底价格
import websocket      # WebSocket客户端：用于接收币安实时行情推送
import json           # JSON解析库：解析API/WS返回的JSON数据
import threading      # 多线程库：让WebSocket在后台独立线程运行，不阻塞主线程
import time           # 时间控制库：用于休眠、时间戳处理（备用）
from collections import deque  # 固定长度队列：缓存K线数据，自动淘汰旧数据
from price_center import update_ws_price
import pyttsx3  # 语音播报
import queue

last_speak_time = {}

# ===============================
# 全局配置区（可修改的参数）
# ===============================
SYMBOL = "XAGUSDT"          # 交易对：白银/USDT（期货）
WS_SYMBOL = SYMBOL.lower()  # WebSocket订阅要求交易对小写（币安官方规范）

# 币安期货HTTP接口基础地址（REST API）
HTTP_BASE = "https://fapi.binance.com"

# WebSocket多流订阅地址（核心🔥：同时订阅多个数据流，用/分隔）
WS_URL = f"wss://fstream.binance.com/stream?streams=" \
         f"{WS_SYMBOL}@aggTrade/" \
         f"{WS_SYMBOL}@markPrice/" \
         f"{WS_SYMBOL}@depth20/" \
         f"{WS_SYMBOL}@kline_1m"

# 需要监控的多周期K线（策略层会用到这些周期）
INTERVALS = ["1m", "5m", "15m", "30m", "1h"]

# 初始化时从HTTP接口加载的历史K线数量（120根足够覆盖大部分指标计算）
INIT_LIMIT = 120

# ===============================
# 全局数据缓存（核心🔥：所有模块共享的行情数据）
# ===============================
cache = {
    "price": None,  # 最新成交价（优先来自aggTrade，备用来自markPrice）
    "depth": None,  # 盘口深度数据（depth20）
    # 各周期K线缓存：使用deque固定长度200，自动淘汰超过200根的旧K线
    "klines": {i: deque(maxlen=200) for i in INTERVALS},
}

# 线程锁（核心🔥：防止多线程同时读写cache导致数据错乱）
lock = threading.Lock()

# WebSocket连接状态标记（全局变量）：True=正常，False=断开/异常
ws_alive = False

# ===============================
# 🔊 语音引擎（放这里👇）
# ===============================
engine = pyttsx3.init()
engine.setProperty('rate', 180)
engine.setProperty('volume', 1.0)

# ===============================
# 🔊 多线程安全语音播报队列
# ===============================
speech_queue = queue.Queue()

def speech_worker():
    while True:
        text = speech_queue.get()
        if text is None:
            break
        engine.say(text)
        engine.runAndWait()
        speech_queue.task_done()

# 启动播报线程
threading.Thread(target=speech_worker, daemon=True).start()

def speak(text, cooldown=10):
    """带冷却的语音播报"""
    now = time.time()
    if text in last_speak_time:
        if now - last_speak_time[text] < cooldown:
            return
    last_speak_time[text] = now
    speech_queue.put(text)

# ===============================
# 初始化K线数据（程序启动时执行）
# ===============================
def init_klines():
    print("📥 初始化K线...")
    for interval in INTERVALS:
        try:
            url = f"{HTTP_BASE}/fapi/v1/klines"
            params = {
                "symbol": SYMBOL,
                "interval": interval,
                "limit": INIT_LIMIT
            }
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

            # 初始化完成检查
            success = True
            with lock:
                for iv in INTERVALS:
                    if len(cache["klines"][iv]) == 0:
                        success = False

            if success:
                print("🎉 K线初始化完成")
                speak("行情数据初始化成功")
            else:
                print("⚠️ K线初始化异常")
                speak("行情初始化失败，请检查接口")

            print(f"✅ {interval} OK")

            # 打印最新1m K线
            if interval == "1m":
                with lock:
                    if cache["klines"]["1m"]:
                        last_1m = cache["klines"]["1m"][-1]
                        print("初始化1m K线最新:", last_1m)

        except Exception as e:
            print(f"❌ {interval} 初始化失败:", e)

# ===============================
# HTTP兜底更新函数
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

    # 兜底更新最新成交价
    try:
        r = requests.get(f"{HTTP_BASE}/fapi/v1/ticker/price",
                         params={"symbol": SYMBOL}, timeout=3)
        with lock:
            cache["price"] = float(r.json()["price"])
    except:
        pass

# ===============================
# WebSocket消息处理
# ===============================
def on_message(ws, message):
    global ws_alive
    ws_alive = True
    msg = json.loads(message)
    data = msg.get("data", msg)

    with lock:
        if data.get("e") == "aggTrade":
            update_ws_price(float(data["p"]))
        elif data.get("e") == "markPriceUpdate":
            if cache["price"] is None:
                update_ws_price(float(data["p"]))
        elif data.get("e") == "depthUpdate":
            cache["depth"] = data
        elif data.get("e") == "kline":
            k = data["k"]
            new_k = {"time": k["t"], "open": float(k["o"]), "high": float(k["h"]),
                     "low": float(k["l"]), "close": float(k["c"]), "volume": float(k["v"])}
            if not cache["klines"]["1m"] or cache["klines"]["1m"][-1]["time"] != new_k["time"]:
                cache["klines"]["1m"].append(new_k)

# ===============================
# WS连接成功回调
# ===============================
def on_open(ws):
    global ws_alive
    ws_alive = True
    print("🟢 WS已连接成功")
    speak("WebSocket连接成功")

# ===============================
# WS错误/关闭处理
# ===============================
def on_error(ws, error):
    global ws_alive
    ws_alive = False
    print("❌ WS错误:", error)
    speak("WebSocket连接错误")

def on_close(ws, *args):
    global ws_alive
    ws_alive = False
    print("⚠️ WS关闭")
    speak("WebSocket已断开")

# ===============================
# 启动WebSocket连接
# ===============================
def start_ws():
    print("🚀 启动WS...")
    ws = websocket.WebSocketApp(
        WS_URL,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
        on_open=on_open
    )
    threading.Thread(target=ws.run_forever, daemon=True).start()

# ===============================
# 调试测试函数
# ===============================
def debug_run():
    print("🧪 启动 market_data 调试模式")
    init_klines()
    with lock:
        print("\n====== 初始化检查 ======")
        for interval in INTERVALS:
            print(f"{interval} K线数量:", len(cache["klines"][interval]))
    start_ws()
    last_price = None
    # 无限循环：持续打印实时状态（调试用）
    while True:
        time.sleep(3)  # 每3秒打印一次，避免输出刷屏

        # 加锁读取缓存（防止WS线程同时写入）
        with lock:
            price = cache["price"]  # 最新价格
            depth = cache["depth"]  # 盘口数据
            kline_1m = cache["klines"]["1m"]  # 1分钟K线

        # 打印实时状态分隔线
        print("\n====== 实时状态 ======")

        # 1. WS连接状态
        print("WS状态:", "🟢 正常" if ws_alive else "🔴 断开")

        # 2. 价格检查（触发HTTP兜底）
        if price is None:
            print("价格: ❌ 未获取（触发HTTP兜底）")
            speak("价格数据异常")
            http_update()
        else:
            print(f"价格未变化: {price}" if price == last_price else f"最新价格: {price}")
            last_price = price

        # 3. 1分钟K线检查
        if kline_1m:
            latest_k = kline_1m[-1]
            print("最新1m K线:")
            print("时间:", latest_k["time"])
            print("收盘:", latest_k["close"])
        else:
            print("❌ 1m K线为空")
            speak("K线数据异常")

        # 4. 盘口深度检查
        if depth:
            bids = depth.get("b", [])
            asks = depth.get("a", [])
            if bids and asks:
                print("盘口:")
                print("买一:", bids[0])
                print("卖一:", asks[0])
            else:
                print("盘口: 数据结构异常")
        else:
            print("盘口: 未获取")

        # 5. WS异常提示
        if not ws_alive:
            print("⚠️ WS异常，建议重启连接（当前未自动重连）")

# ===============================
# 程序入口
# ===============================
if __name__ == "__main__":
    debug_run()