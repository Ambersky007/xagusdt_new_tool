import yfinance as yf
import math

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