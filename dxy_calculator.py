import yfinance as yf
import math
import pandas as pd

# 存储实时价格
prices = {}

def on_message(msg):
    try:
        symbol = msg['id']
        price = float(msg['price'])

        prices[symbol] = price

        if len(prices) == 6:
            calculate_dxy()

    except Exception as e:
        print("解析错误:", e)


def calculate_dxy():
    try:
        eurusd = prices['EURUSD=X']
        usdjpy = prices['JPY=X']
        gbpusd = prices['GBPUSD=X']
        usdcad = prices['CAD=X']
        usdsek = prices['SEK=X']
        usdchf = prices['CHF=X']

        dxy = 50.14348112 * (
            (eurusd ** -0.576) *
            (usdjpy ** 0.136) *
            (gbpusd ** -0.119) *
            (usdcad ** 0.091) *
            (usdsek ** 0.042) *
            (usdchf ** 0.036)
        )

        print(f"美元指数 DXY: {dxy:.4f}")

    except Exception as e:
        print("计算错误:", e)


ws = yf.WebSocket()

ws.subscribe([
    'EURUSD=X',
    'JPY=X',
    'GBPUSD=X',
    'CAD=X',
    'SEK=X',
    'CHF=X'
])

ws.listen(on_message)

dxy_history = []  # 保存历史DXY数值，长度可控

def update_dxy(dxy_value):
    """
    更新DXY数据并计算趋势信号
    dxy_value: 实时DXY
    """
    global dxy_history
    dxy_history.append(dxy_value)

    # 限制历史长度，避免内存无限增长
    MAX_LEN = 200
    if len(dxy_history) > MAX_LEN:
        dxy_history = dxy_history[-MAX_LEN:]

    # 转成Series便于EMA计算
    s = pd.Series(dxy_history)

    short_period = 5   # 短期平滑
    long_period = 20   # 长期趋势

    ema_short = s.ewm(span=short_period).mean().iloc[-1]
    ema_long = s.ewm(span=long_period).mean().iloc[-1]

    # 趋势信号
    if ema_short > ema_long:
        trend = "UP"
    elif ema_short < ema_long:
        trend = "DOWN"
    else:
        trend = "FLAT"

    return trend, ema_short, ema_long


trend_buffer = []  # 保存最近N个趋势

def filtered_trend(trend, confirm_n=3):
    trend_buffer.append(trend)
    if len(trend_buffer) > confirm_n:
        trend_buffer.pop(0)
    # 如果连续 confirm_n 次趋势一致，才返回，否则返回None
    if len(set(trend_buffer)) == 1:
        return trend_buffer[0]
    return None