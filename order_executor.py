# ===============================
# order_executor.py
# 下单功能模块（支持剃头皮/追单、止盈止损、滑点、订单管理）
# ===============================

import requests  # 用于发送HTTP请求
import time      # 时间戳与延时控制
import hmac      # HMAC签名
import hashlib   # SHA256
from config_account import TRADE_MODE, REAL_ACCOUNT, TEST_ACCOUNT  # 获取账户信息
import time

# 全局订单列表，用于止盈止损监控
order_list = []

# 默认滑点设置（0.01‰）
DEFAULT_SLIPPAGE = 0.0001

def get_account():
    """
    根据 TRADE_MODE 返回当前使用的账户
    """
    return TEST_ACCOUNT if TRADE_MODE == "TEST" else REAL_ACCOUNT

def get_market_price(symbol):
    """
    获取当前市场价格
    TODO: 可改为 WS/HTTP 获取实时价格
    """
    return 70.0  # 临时返回一个固定价格用于测试

def place_order(symbol, side, quantity, price=None, order_type="NORMAL",
                scalping_range=None, stop_loss=None, take_profit=None,
                slippage=DEFAULT_SLIPPAGE):
    """
    下单函数（市价单/限价单）
    side: "BUY" 或 "SELL"
    quantity: 下单数量
    price: 限价价格，可为 None 使用市价
    order_type: "SCALPING" 或 "NORMAL"
    scalping_range: 剃头皮区间 (low, high)
    stop_loss / take_profit: 可选止损止盈
    slippage: 滑点限制
    """
    account = get_account()  # 获取当前账户

    # -----------------------------
    # 剃头皮限价单逻辑
    # -----------------------------
    if order_type == "SCALPING" and scalping_range:
        low, high = scalping_range
        if side == "BUY":
            price = low - 0.02  # 向上剃头皮做多，价格 = 区间低 - 0.02
        elif side == "SELL":
            price = high + 0.02  # 向下剃头皮做空，价格 = 区间高 + 0.02

    # -----------------------------
    # 滑点控制
    # -----------------------------
    market_price = get_market_price(symbol)  # 当前市场价格
    if price:
        if side == "BUY" and price > market_price * (1 + slippage):
            print(f"⚠️ 买入价格超滑点限制 {price} > {market_price*(1+slippage)}")
            return None
        elif side == "SELL" and price < market_price * (1 - slippage):
            print(f"⚠️ 卖出价格超滑点限制 {price} < {market_price*(1-slippage)}")
            return None
    else:
        price = market_price  # 如果没指定价格，直接用市价

    # -----------------------------
    # 构造下单请求
    # -----------------------------
    url = f"{account['BASE_URL']}/fapi/v1/order"  # 下单接口
    headers = {"X-MBX-APIKEY": account["API_KEY"]}  # 请求头

    data = {
        "symbol": symbol,
        "side": side,
        "type": "MARKET" if price is None else "LIMIT",
        "quantity": quantity,
        "timestamp": int(time.time() * 1000)
    }
    if price:
        data["price"] = price
        data["timeInForce"] = "GTC"

    # -----------------------------
    # 生成签名
    # -----------------------------
    query_string = "&".join([f"{k}={v}" for k, v in data.items()])
    signature = hmac.new(account["API_SECRET"].encode("utf-8"),
                         query_string.encode("utf-8"),
                         hashlib.sha256).hexdigest()
    data["signature"] = signature

    # -----------------------------
    # 发送下单请求
    # -----------------------------
    try:
        r = requests.post(url, headers=headers, data=data, timeout=5)
        res = r.json()
        print(f"💰 [{TRADE_MODE}] 下单返回:", res)

        # -----------------------------
        # 记录订单信息
        # -----------------------------
        order = {
            "order_id": res.get("orderId", f"temp_{int(time.time())}"),
            "symbol": symbol,
            "side": side,
            "qty": quantity,
            "price": price,
            "order_type": order_type,
            "scalping_range": scalping_range,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "max_profit": 0.0  # 用于记录开单以来最大浮动盈利
        }
        order_list.append(order)
        return res
    except Exception as e:
        print("❌ 下单失败:", e)
        return None

def check_stop_orders():
    """
    遍历全局订单列表，触发止盈止损
    支持剃头皮止盈回落和平仓逻辑
    """
    global order_list
    to_remove = []

    for order in order_list:
        symbol = order["symbol"]
        side = order["side"]
        price = get_market_price(symbol)

        # 计算浮动盈利
        if side == "BUY":
            profit = price - order["price"]
        else:
            profit = order["price"] - price

        # 更新最大浮动盈利
        if profit > order["max_profit"]:
            order["max_profit"] = profit

        # -----------------------------
        # 剃头皮止盈/止损逻辑
        # -----------------------------
        if order["order_type"] == "SCALPING" and order.get("scalping_range"):
            low, high = order["scalping_range"]
            # 向上剃头皮做多
            if side == "BUY":
                # 止盈：盈利>=0.05%且回落到最大盈利的80%
                if profit >= 0.0005*order["price"] and profit <= order["max_profit"]*0.8:
                    print(f"🎯 向上剃头皮止盈回落, 平仓 {symbol}")
                    place_order(symbol, "SELL", order["qty"])
                    to_remove.append(order)
                # 止损：价格 < 区间低点立即平仓
                elif price <= low:
                    print(f"⚠️ 向上剃头皮止损触发, 平仓 {symbol}")
                    place_order(symbol, "SELL", order["qty"])
                    to_remove.append(order)
            # 向下剃头皮做空
            elif side == "SELL":
                # 止盈：盈利>=0.05%且回落到最大盈利的80%
                if profit >= 0.0005*order["price"] and profit <= order["max_profit"]*0.8:
                    print(f"🎯 向下剃头皮止盈回落, 平仓 {symbol}")
                    place_order(symbol, "BUY", order["qty"])
                    to_remove.append(order)
                # 止损：价格 > 区间高点立即平仓
                elif price >= high:
                    print(f"⚠️ 向下剃头皮止损触发, 平仓 {symbol}")
                    place_order(symbol, "BUY", order["qty"])
                    to_remove.append(order)

        # -----------------------------
        # 追单止盈/止损逻辑
        # -----------------------------
        if order["order_type"] == "NORMAL":
            # 多头追多
            if side == "BUY":
                if profit >= 0.0005*order["price"] and profit <= order["max_profit"]*0.8:
                    print(f"🎯 追多止盈回落, 平仓 {symbol}")
                    place_order(symbol, "SELL", order["qty"])
                    to_remove.append(order)
                # 止损：价格 < 下单价 - 0.7
                elif price <= order["price"] - 0.7:
                    print(f"⚠️ 追多止损触发, 平仓 {symbol}")
                    place_order(symbol, "SELL", order["qty"])
                    to_remove.append(order)
            # 空头追空
            elif side == "SELL":
                if profit >= 0.0005*order["price"] and profit <= order["max_profit"]*0.8:
                    print(f"🎯 追空止盈回落, 平仓 {symbol}")
                    place_order(symbol, "BUY", order["qty"])
                    to_remove.append(order)
                # 止损：价格 > 下单价 + 0.7
                elif price >= order["price"] + 0.7:
                    print(f"⚠️ 追空止损触发, 平仓 {symbol}")
                    place_order(symbol, "BUY", order["qty"])
                    to_remove.append(order)

    # -----------------------------
    # 移除已触发的订单
    # -----------------------------
    for o in to_remove:
        order_list.remove(o)