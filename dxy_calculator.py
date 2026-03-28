import math
import pandas as pd
import time

# 存储实时价格
prices = {}
dxy_history = []
MAX_LEN = 200
trend_buffer = []


def on_message(msg):
    try:
        symbol = msg['id']
        price = float(msg['price'])
        prices[symbol] = price

        if len(prices) == 6:
            dxy = calculate_dxy()
            if dxy:
                update_dxy(dxy)
    except Exception:
        pass


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
        return dxy
    except Exception:
        return None


def update_dxy(dxy_value):
    global dxy_history
    if dxy_value is None:
        return "FLAT", 0, 0

    dxy_history.append(dxy_value)
    if len(dxy_history) > MAX_LEN:
        dxy_history = dxy_history[-MAX_LEN:]

    if len(dxy_history) < 2:
        return "FLAT", 0, 0

    prev = dxy_history[-2]
    change_pct = (dxy_value - prev) / prev * 100
    strength = abs(change_pct)

    s = pd.Series(dxy_history)
    ema_short = s.ewm(span=5).mean().iloc[-1]
    ema_long = s.ewm(span=20).mean().iloc[-1]

    if ema_short > ema_long:
        base_trend = "UP"
    elif ema_short < ema_long:
        base_trend = "DOWN"
    else:
        base_trend = "FLAT"

    if strength < 0.05:
        strength_level = "FLAT"
    elif strength < 0.2:
        strength_level = "WEAK"
    else:
        strength_level = "STRONG"

    final_trend = f"{strength_level}_{base_trend}" if base_trend != "FLAT" else "FLAT"
    return final_trend, ema_short, ema_long


def filtered_trend(trend, confirm_n=3):
    if not trend:
        return None

    trend_buffer.append(trend)
    if len(trend_buffer) > confirm_n:
        trend_buffer.pop(0)

    if len(set(trend_buffer)) == 1:
        return trend_buffer[0]
    return None


# ===========================
# 🔁 持续测试函数（你要的）
# ===========================
def test_dxy_continuous():
    print("=" * 70)
    print("🧪 DXY 持续测试模式（每 1 秒刷新）")
    print("=" * 70)

    # 模拟初始货币价格
    test_prices = {
        "EURUSD=X": 1.0800,
        "JPY=X": 148.00,
        "GBPUSD=X": 1.2600,
        "CAD=X": 1.3500,
        "SEK=X": 10.800,
        "CHF=X": 0.8700
    }

    global prices
    prices.update(test_prices)

    while True:
        dxy = calculate_dxy()
        if not dxy:
            print("❌ DXY 计算失败")
            time.sleep(1)
            continue

        trend, ema5, ema20 = update_dxy(dxy)
        confirmed = filtered_trend(trend)

        # 🔥 持续打印
        print(f"\r💵 DXY: {dxy:7.2f} | 趋势: {trend:12} | 确认: {confirmed} | EMA5: {ema5:.2f} EMA20: {ema20:.2f}", end="")

        time.sleep(1)


if __name__ == "__main__":
    test_dxy_continuous()