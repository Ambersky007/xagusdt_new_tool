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
"""

# ===============================
# 导入所需的核心库
# ===============================
import requests
import websocket
import json
import threading
import time
import pandas as pd
import numpy as np
from collections import deque

# 外部模块导入 - 修复：将has_position改为get_position
from order_executor import place_order, check_stop_orders, get_position  # 修复：has_position → get_position
from config_account import DEFAULT_ORDER_QTY, SYMBOL
from voice_alert import speak
from price_center import update_ws_price, get_price

# ===============================
# 配置区 - 策略核心参数配置
# ===============================
SYMBOL = "XAGUSDT"
WS_SYMBOL = SYMBOL.lower()

# API基础地址配置
HTTP_BASE = "https://fapi.binance.com"

# WebSocket多流订阅地址
WS_URL = f"wss://fstream.binance.com/stream?streams={WS_SYMBOL}@aggTrade/{WS_SYMBOL}@markPrice/{WS_SYMBOL}@depth20/{WS_SYMBOL}@kline_1m"

# 策略监控的K线周期
INTERVALS = ["1m", "5m", "15m", "30m", "1h"]
INIT_LIMIT = 120

# WebSocket兜底配置
WS_TIMEOUT_SECONDS = 10
last_ws_message_ts = 0

# 防重复下单配置
ORDER_COOLDOWN = 60
last_signal = None
last_order_time = 0

# ===============================
# 数据缓存区
# ===============================
cache = {
    "depth": {"bid": None, "ask": None},
    "klines": {i: deque(maxlen=200) for i in INTERVALS},
}

lock = threading.Lock()
ws_alive = False


# ===============================
# 初始化K线数据
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
                        "time": k[0],
                        "open": float(k[1]),
                        "high": float(k[2]),
                        "low": float(k[3]),
                        "close": float(k[4]),
                        "volume": float(k[5])
                    })
            print(f"✅ {interval} K线初始化成功")
        except Exception as e:
            print(f"❌ {interval} K线初始化失败: {e}")


# ===============================
# HTTP兜底更新函数
# ===============================
def http_update():
    print("🔄 执行HTTP兜底更新...")
    url = f"{HTTP_BASE}/fapi/v1/klines"

    for interval in INTERVALS:
        try:
            if not cache["klines"][interval]:
                continue
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
                print(f"📈 {interval} K线已更新")
        except Exception as e:
            print(f"⚠️ {interval} K线更新失败: {e}")

    # 价格兜底更新
    try:
        price_url = f"{HTTP_BASE}/fapi/v1/ticker/price"
        r = requests.get(price_url, params={"symbol": SYMBOL}, timeout=3)
        price = float(r.json()["price"])
        update_ws_price(price)
        print(f"💰 价格更新为: {get_price():.4f}")
    except Exception as e:
        print(f"⚠️ 价格更新失败: {e}")

    # 盘口数据兜底更新
    try:
        depth_url = f"{HTTP_BASE}/fapi/v1/depth"
        r = requests.get(depth_url, params={"symbol": SYMBOL, "limit": 1}, timeout=3)
        depth_data = r.json()

        with lock:
            cache["depth"]["bid"] = float(depth_data["bids"][0][0]) if (
                    depth_data.get("bids") and len(depth_data["bids"]) > 0) else None
            cache["depth"]["ask"] = float(depth_data["asks"][0][0]) if (
                    depth_data.get("asks") and len(depth_data["asks"]) > 0) else None

        bid_str = f"{cache['depth']['bid']:.4f}" if cache['depth']['bid'] else "N/A"
        ask_str = f"{cache['depth']['ask']:.4f}" if cache['depth']['ask'] else "N/A"
        print(f"📊 盘口更新 - 买一: {bid_str} | 卖一: {ask_str}")
    except Exception as e:
        print(f"⚠️ 盘口更新失败: {str(e)}")


# ===============================
# 技术指标计算函数
# ===============================
def calc_indicators(df):
    if len(df) < 30:
        return None

    close = df["close"]

    # MACD计算
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()

    # KDJ计算
    low_min = df["low"].rolling(window=9).min()
    high_max = df["high"].rolling(window=9).max()
    rsv = (close - low_min) / (high_max - low_min + 1e-9) * 100
    k = rsv.ewm(com=2, adjust=False).mean()
    d = k.ewm(com=2, adjust=False).mean()
    j = 3 * k - 2 * d

    # ATR计算
    atr = (df["high"] - df["low"]).rolling(window=14).mean()

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
# ===============================
def analyze_trend(df):
    if len(df) < 30:
        return "数据不足"

    close = df["close"]
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()

    last_n = 3
    scores = []
    for i in range(-last_n, 0):
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
# WebSocket消息处理函数
# ===============================
def on_message(ws, message):
    global ws_alive, last_ws_message_ts
    ws_alive = True
    last_ws_message_ts = time.time()

    msg = json.loads(message)
    data = msg.get("data", msg)

    with lock:
        if data.get("e") == "aggTrade":
            price = float(data["p"])
            update_ws_price(price)

        elif data.get("e") == "markPriceUpdate":
            current_price = get_price()
            if current_price is None:
                price = float(data["p"])
                update_ws_price(price)

        elif data.get("e") == "depthUpdate":
            cache["depth"]["bid"] = float(data["b"][0][0]) if (data.get("b") and len(data["b"]) > 0) else None
            cache["depth"]["ask"] = float(data["a"][0][0]) if (data.get("a") and len(data["a"]) > 0) else None

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

            if cache["klines"]["1m"] and cache["klines"]["1m"][-1]["time"] != new_k["time"]:
                cache["klines"]["1m"].append(new_k)


# ===============================
# WebSocket错误/关闭处理函数
# ===============================
def on_error(ws, error):
    global ws_alive
    ws_alive = False
    print(f"❌ WebSocket错误: {error}")


def on_close(ws, *args):
    global ws_alive
    ws_alive = False
    print("⚠️ WebSocket连接关闭")


# ===============================
# 启动WebSocket连接
# ===============================
def start_ws():
    global last_ws_message_ts
    print("🚀 启动WebSocket连接...")
    last_ws_message_ts = time.time()

    ws = websocket.WebSocketApp(
        WS_URL,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close
    )
    threading.Thread(target=ws.run_forever, daemon=True).start()


# ===============================
# ATR波动过滤器
# ===============================
def atr_filter(df, name=""):
    if len(df) < 50:
        return f"{name} 数据不足"

    atr = (df["high"] - df["low"]).rolling(window=14).mean()
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
# 1分钟周期剃头皮震荡识别
# ===============================
def detect_scalping_range(df):
    if len(df) < 20:
        return "数据不足", None, None

    recent = df.tail(15)
    range_high = recent["high"].max()
    range_low = recent["low"].min()
    mean_price = recent["close"].mean()
    range_pct = (range_high - range_low) / mean_price

    if range_pct > 0.002:
        return "非震荡（波动过大）", None, None

    touch_high = (recent["high"] > range_high * 0.999).sum()
    touch_low = (recent["low"] < range_low * 1.001).sum()

    if touch_high >= 2 and touch_low >= 2:
        return "🟡 可剃头皮", range_high, range_low
    else:
        return "震荡不足", None, None


# ===============================
# 剃头皮专用ATR过滤
# ===============================
def atr_filter_scalping(df):
    if len(df) < 50:
        return "❌ 数据不足"

    atr = (df["high"] - df["low"]).rolling(window=14).mean()
    current_atr = atr.iloc[-1]
    atr_mean = atr.rolling(50).mean().iloc[-1]

    if atr_mean == 0 or np.isnan(atr_mean):
        return "❌ 数据异常"

    if current_atr >= atr_mean * 1.8:
        return "❌ 波动过大"
    elif current_atr <= atr_mean * 0.6:
        return "❌ 波动过小"
    else:
        return "✅ 可剃头皮"


# ===============================
# 成交量相关判断函数
# ===============================
def is_low_volume(df):
    if len(df) < 60:
        return False
    vol = df["volume"]
    last_vol = vol.iloc[-2]
    lowest_5 = vol.nsmallest(5)
    avg_low = lowest_5.mean()
    return avg_low * 0.85 <= last_vol <= avg_low * 1.15


def is_volume_expand(df):
    if len(df) < 2:
        return False
    vol = df["volume"]
    return vol.iloc[-1] > vol.iloc[-2] * 3


# ===============================
# MACD金叉/死叉检测
# ===============================
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
# ATR最终交易判断函数
# ===============================
def can_trade(atr_1m_status, atr_5m_status):
    if "过大" in atr_1m_status:
        return "❌ 禁止交易（短期波动过大）"
    if "过小" in atr_5m_status:
        return "❌ 禁止交易（市场太平）"
    return "✅ 可以交易"


# ===============================
# 监控主函数
# ===============================
def monitor():
    global last_ws_message_ts, last_signal, last_order_time
    while True:
        time.sleep(5)

        # WS兜底判断
        current_ts = time.time()
        ws_timeout = current_ts - last_ws_message_ts > WS_TIMEOUT_SECONDS
        if not ws_alive or ws_timeout:
            if ws_timeout:
                print(f"⚠️ WebSocket无消息超时（{WS_TIMEOUT_SECONDS}秒）→ 切换至HTTP兜底")
            else:
                print("⚠️ WebSocket不可用 → 切换至HTTP兜底")
            http_update()
            last_ws_message_ts = current_ts

        # 价格为空强制更新
        if get_price() is None:
            http_update()

        # 读取缓存数据
        with lock:
            df_1m = pd.DataFrame(cache["klines"]["1m"])
            df_5m = pd.DataFrame(cache["klines"]["5m"])
            df_15m = pd.DataFrame(cache["klines"]["15m"])
            df_1h = pd.DataFrame(cache["klines"]["1h"])
            bid_price = cache["depth"]["bid"]
            ask_price = cache["depth"]["ask"]

        if df_1m.empty:
            continue

        # 全局持仓判断（最高优先级）- 修复：将has_position改为get_position
        try:
            # 新增：先查询持仓并保存结果
            has_position = get_position(SYMBOL)
            # 新增：打印持仓查询结果
            print(f"\n🔍 持仓查询结果 - {SYMBOL}: {'✅ 有持仓' if has_position else '❌ 无持仓'}")

            if has_position:  # 修复：has_position → get_position
                entry_signal = "⛔ 已有持仓，不开新单"
                print(f"⚠️ 检测到{SYMBOL}已有持仓，本次循环跳过所有交易信号计算")
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
            # 新增：持仓查询失败也明确打印
            print(f"🔍 持仓查询结果 - {SYMBOL}: ❌ 查询失败")
            entry_signal = "⛔ 持仓判断异常，暂停交易"
            print("\n" + "=" * 30)
            print(f"最终信号: {entry_signal}")
            print(f"下单状态: 未下单")
            print("=" * 30 + "\n")
            continue

        # 基础数据计算
        indicators = calc_indicators(df_1m)
        vol_1m = df_1m["volume"]
        current_price = get_price()
        if current_price is None:
            print("⚠️ 获取价格失败，跳过")
            continue

        # ATR双周期过滤
        atr_1m_status = atr_filter(df_1m, "1m")
        atr_5m_status = atr_filter(df_5m, "5m")
        trade_status = can_trade(atr_1m_status, atr_5m_status)

        # 多周期趋势分析
        trend_5m = analyze_trend(df_5m)
        trend_15m = analyze_trend(df_15m)
        trend_1h = analyze_trend(df_1h)

        # 剃头皮策略核心判断
        scalping_range_status, range_high, range_low = detect_scalping_range(df_1m)
        scalping_atr_status = atr_filter_scalping(df_1m)
        golden_cross = is_macd_golden_cross(df_5m)
        dead_cross = is_macd_dead_cross(df_5m)

        scalp_signal_up = (
                "可剃头皮" in scalping_range_status and
                "可剃头皮" in scalping_atr_status and
                golden_cross
        )
        scalp_signal_down = (
                "可剃头皮" in scalping_range_status and
                "可剃头皮" in scalping_atr_status and
                dead_cross
        )

        # 价格边界判断
        near_low = False
        near_high = False
        if range_low and range_high:
            near_low = current_price <= range_low * 0.999
            near_high = current_price >= range_high * 1.001

        # 趋势追单判断
        low_vol = is_low_volume(df_1m)
        vol_expand = is_volume_expand(df_1m)
        trend_long = (
                trend_15m in ["🟢 强多", "🟡 偏多"] and
                trend_1h in ["🟢 强多", "🟡 偏多"]
        )
        trend_short = (
                trend_15m in ["🔴 强空", "🟠 偏空"] and
                trend_1h in ["🔴 强空", "🟠 偏空"]
        )

        if trend_long and golden_cross and low_vol and vol_expand:
            trend_signal = "🔵 追多"
        elif trend_short and dead_cross and low_vol and vol_expand:
            trend_signal = "🔴 追空"
        else:
            trend_signal = False

        # 最终交易信号生成
        entry_signal = "⛔ 不交易"
        if scalp_signal_up and near_low:
            entry_signal = "🟢 向上剃头皮"
        elif scalp_signal_down and near_high:
            entry_signal = "🟠 向下剃头皮"
        elif trend_signal:
            entry_signal = trend_signal

        # 风险过滤
        ema20 = df_1m["close"].ewm(span=20, adjust=False).mean().iloc[-1]
        if abs(current_price - ema20) / current_price > 0.002:
            entry_signal = "⛔ 偏离过大，不追"
        if "禁止交易" in trade_status:
            entry_signal = "⛔ ATR风控：禁止交易"
        if bid_price and ask_price:
            spread_pct = (ask_price - bid_price) / bid_price
            if spread_pct > 0.001:
                entry_signal = "⛔ 盘口价差过大，不交易"

        # 防重复下单
        now = time.time()
        if entry_signal != "⛔ 不交易" and entry_signal == last_signal and now - last_order_time < ORDER_COOLDOWN:
            entry_signal = f"⛔ 信号重复，{int(ORDER_COOLDOWN - (now - last_order_time))}秒后可交易"

        # 止损止盈方向计算（精准匹配）
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
                stop_loss_value = current_price - atr_value * 1.5  # 多单止损：价格下方
                take_profit_value = current_price + atr_value * 3  # 多单止盈：价格上方
            elif is_short:
                stop_loss_value = current_price + atr_value * 1.5  # 空单止损：价格上方
                take_profit_value = current_price - atr_value * 3  # 空单止盈：价格下方

        # 下单执行
        if entry_signal in long_signals:  # 多单信号
            success = place_order(
                symbol=SYMBOL,
                side="BUY",
                quantity=DEFAULT_ORDER_QTY,
                order_type="SCALPING" if "剃头皮" in entry_signal else "NORMAL",
                scalping_range=(range_low, range_high) if "剃头皮" in entry_signal else None,
                stop_loss=stop_loss_value,
                take_profit=take_profit_value
            )
            if success:
                order_status = "已下多单"
                last_signal = entry_signal
                last_order_time = now
                speak(f"已下多单，策略：{entry_signal}", key="order", force=True)

        elif entry_signal in short_signals:  # 空单信号
            success = place_order(
                symbol=SYMBOL,
                side="SELL",
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

        # 打印策略信息
        print("\n" + "=" * 30)
        print("📊 实时数据")
        print(f"价格: {current_price:.4f}")
        bid_str = f"{bid_price:.4f}" if bid_price is not None else "N/A"
        ask_str = f"{ask_price:.4f}" if ask_price is not None else "N/A"
        print(f"盘口 - 买一: {bid_str} | 卖一: {ask_str}")
        print(f"成交量均值: {vol_1m.mean():.2f} | 最新成交量: {vol_1m.iloc[-1]:.2f}")
        print(f"最后WS消息时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(last_ws_message_ts))}")
        # 新增：在实时数据区也打印持仓结果
        print(f"持仓状态: {'✅ 有持仓' if get_position(SYMBOL) else '❌ 无持仓'}")

        print("\n📈 多周期趋势")
        print(f"5分钟: {trend_5m} | 15分钟: {trend_15m} | 1小时: {trend_1h}")
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
# ===============================
def main():
    print("🚀 币安期货交易策略系统启动")
    init_klines()
    start_ws()
    check_stop_orders(SYMBOL)  # 修复：补充缺失的参数SYMBOL
    monitor()


if __name__ == "__main__":
    main()