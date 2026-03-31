# ===============================
# order_executor.py
# 最终实盘修复版（仅修复你指出的5大高危问题）
# ===============================
import requests
import time
import hmac
import hashlib

from config_account import (
    TRADE_MODE, REAL_ACCOUNT, TEST_ACCOUNT,
    API_KEY, API_SECRET, SYMBOL, BASE_URL
)
from price_center import get_price
from time_sync import time_offset

order_list = []
DEFAULT_SLIPPAGE = 0.0001
MAX_PRICE_DEVIATION = 0.01


# ===============================
# 统一时间生成
# ===============================
def get_server_time():
    ts = int(time.time() * 1000) + time_offset
    return ts, 5000


def get_account():
    return TEST_ACCOUNT if TRADE_MODE == "TEST" else REAL_ACCOUNT


def format_quantity(qty, step_size=0.001):
    return round(int(qty / step_size) * step_size, 3)


# ===============================
# 下单函数（修复 1 / 3 / 5）
# ===============================
def place_order(symbol, side, quantity, price=None, order_type="NORMAL",
                scalping_range=None, stop_loss=None, take_profit=None,
                slippage=DEFAULT_SLIPPAGE,
                real_order_type="MARKET",
                reduce_only=False):
    account = get_account()
    quantity = format_quantity(quantity)

    market_price = get_price()
    if market_price is None:
        print("❌ 获取实时价格失败，拒绝下单")
        return None

    # ===============================
    # 修复 5：市价单滑点保护（平仓专用）
    # ===============================
    if real_order_type == "MARKET":
        current_price = get_price()
        if current_price is None:
            print("❌ 市价单价格获取失败")
            return None
        slippage_rate = abs(current_price - market_price) / market_price
        if slippage_rate > 0.002:
            print(f"⚠️ 市价滑点过高 {slippage_rate:.2%}，拒绝执行")
            return None

    # ===============================
    # 修复 3：剃头皮动态偏移（取代写死±0.02）
    # ===============================
    if order_type == "SCALPING" and scalping_range:
        low, high = scalping_range
        atr_range = abs(high - low)
        offset = max(atr_range * 0.1, 0.01)

        if side == "BUY":
            price = low - offset
        elif side == "SELL":
            price = high + offset

        real_order_type = "LIMIT"

        if side == "BUY" and price > market_price + MAX_PRICE_DEVIATION:
            print(f"❌ 剃头皮买价超滑点 {price}，禁止下单")
            return None
        if side == "SELL" and price < market_price - MAX_PRICE_DEVIATION:
            print(f"❌ 剃头皮卖价超滑点 {price}，禁止下单")
            return None

    if price is not None and real_order_type == "LIMIT":
        if side == "BUY" and price > market_price + MAX_PRICE_DEVIATION:
            print(f"⚠️ LIMIT买价超滑点，拒绝下单")
            return None
        if side == "SELL" and price < market_price - MAX_PRICE_DEVIATION:
            print(f"⚠️ LIMIT卖价超滑点，拒绝下单")
            return None

    ts, recv_win = get_server_time()
    url = f"{account['BASE_URL']}/fapi/v1/order"
    headers = {"X-MBX-APIKEY": account["API_KEY"]}

    data = {
        "symbol": symbol,
        "side": side,
        "type": real_order_type,
        "quantity": quantity,
        "timestamp": ts,
        "recvWindow": recv_win,
        "reduceOnly": "true" if reduce_only else "false"
    }

    # ===============================
    # 修复 1：币安U本位 postOnly → GTX
    # ===============================
    if real_order_type == "LIMIT" and price is not None:
        data["price"] = price
        data["timeInForce"] = "GTX"  # 币安合约专用：只挂单不吃单

    query_string = "&".join([f"{k}={v}" for k, v in data.items()])
    signature = hmac.new(
        account["API_SECRET"].encode(),
        query_string.encode(),
        hashlib.sha256
    ).hexdigest()
    data["signature"] = signature

    try:
        r = requests.post(url, headers=headers, data=data, timeout=5)
        res = r.json()
        print(f"💰 [{TRADE_MODE}] {real_order_type} 下单返回：{res}")

        if res.get("orderId") and not reduce_only:
            order_info = {
                "symbol": symbol,
                "side": side,
                "price": price if price else market_price,
                "qty": quantity,
                "order_id": res["orderId"],
                "order_type": order_type,
                "scalping_range": scalping_range,
                "max_profit": 0.0,
                # ===============================
                # 修复 4：订单绑定唯一标识
                # ===============================
                "position_key": f"{symbol}_{int(time.time())}"
            }
            order_list.append(order_info)
            print(f"📌 订单{res['orderId']}已加入止盈止损监控")
        return res
    except Exception as e:
        print("❌ 下单网络异常：", str(e))
        return None


# ===============================
# 止盈止损（修复 2：仓位方向校验）
# ===============================
def check_stop_orders(symbol):
    global order_list
    to_remove = []
    current_pos = get_position(symbol)

    for order in order_list:
        if order["symbol"] != symbol:
            continue

        if not current_pos:
            print(f"⚠️ {symbol}无持仓，跳过订单{order['order_id']}平仓")
            to_remove.append(order)
            continue

        # ===============================
        # 修复 2：反向持仓直接跳过（防爆仓）
        # ===============================
        if current_pos == "LONG" and order["side"] == "SELL":
            continue
        if current_pos == "SHORT" and order["side"] == "BUY":
            continue

        side = order["side"]
        price = get_price()
        if price is None:
            print("❌ 行情中断，暂停止盈止损判断")
            continue

        if side == "BUY":
            profit = price - order["price"]
        else:
            profit = order["price"] - price

        if profit > order["max_profit"]:
            order["max_profit"] = profit

        # 剃头皮止盈止损
        if order["order_type"] == "SCALPING" and order.get("scalping_range"):
            low, high = order["scalping_range"]
            if side == "BUY":
                if profit >= 0.0005 * order["price"] and profit <= order["max_profit"] * 0.8:
                    print(f"🎯 {symbol}剃头皮多单止盈回落平仓")
                    place_order(symbol, "SELL", order["qty"], real_order_type="MARKET", reduce_only=True)
                    to_remove.append(order)
                elif price <= low:
                    print(f"⚠️ {symbol}剃头皮多单区间止损平仓")
                    place_order(symbol, "SELL", order["qty"], real_order_type="MARKET", reduce_only=True)
                    to_remove.append(order)
            elif side == "SELL":
                if profit >= 0.0005 * order["price"] and profit <= order["max_profit"] * 0.8:
                    print(f"🎯 {symbol}剃头皮空单止盈回落平仓")
                    place_order(symbol, "BUY", order["qty"], real_order_type="MARKET", reduce_only=True)
                    to_remove.append(order)
                elif price >= high:
                    print(f"⚠️ {symbol}剃头皮空单区间止损平仓")
                    place_order(symbol, "BUY", order["qty"], real_order_type="MARKET", reduce_only=True)
                    to_remove.append(order)

        # 普通追单止盈止损
        if order["order_type"] == "NORMAL":
            if side == "BUY":
                if profit >= 0.0005 * order["price"] and profit <= order["max_profit"] * 0.8:
                    print(f"🎯 {symbol}追多止盈回落平仓")
                    place_order(symbol, "SELL", order["qty"], real_order_type="MARKET", reduce_only=True)
                    to_remove.append(order)
                elif price <= order["price"] - 0.7:
                    print(f"⚠️ {symbol}追多固定止损平仓")
                    place_order(symbol, "SELL", order["qty"], real_order_type="MARKET", reduce_only=True)
                    to_remove.append(order)
            elif side == "SELL":
                if profit >= 0.0005 * order["price"] and profit <= order["max_profit"] * 0.8:
                    print(f"🎯 {symbol}追空止盈回落平仓")
                    place_order(symbol, "BUY", order["qty"], real_order_type="MARKET", reduce_only=True)
                    to_remove.append(order)
                elif price >= order["price"] + 0.7:
                    print(f"⚠️ {symbol}追空固定止损平仓")
                    place_order(symbol, "BUY", order["qty"], real_order_type="MARKET", reduce_only=True)
                    to_remove.append(order)

    order_list = [o for o in order_list if o not in to_remove]
    if to_remove:
        print(f"ℹ️ {symbol}本次触发{len(to_remove)}笔止盈止损")
    else:
        print(f"ℹ️ {symbol}暂无止盈止损触发")


# ===============================
# 持仓查询
# ===============================
def get_position(symbol):
    account = get_account()
    try:
        endpoint = "/fapi/v2/positionRisk"
        ts, recv_win = get_server_time()
        query_string = f"timestamp={ts}&recvWindow={recv_win}"
        signature = hmac.new(
            account["API_SECRET"].encode(),
            query_string.encode(),
            hashlib.sha256
        ).hexdigest()
        url = f"{account['BASE_URL']}{endpoint}?{query_string}&signature={signature}"
        headers = {"X-MBX-APIKEY": account["API_KEY"]}

        response = requests.get(url, headers=headers, timeout=3)
        data = response.json()

        if isinstance(data, dict) and "code" in data:
            print(f"❌ 持仓API报错：{data.get('msg')}")
            return None

        for pos in data:
            if pos["symbol"] == symbol:
                amt = float(pos["positionAmt"])
                if amt > 0:
                    print(f"✅ {symbol} 多头持仓：{amt}")
                    return "LONG"
                elif amt < 0:
                    print(f"✅ {symbol} 空头持仓：{abs(amt)}")
                    return "SHORT"
        print(f"ℹ️ {symbol} 当前无持仓")
        return None
    except Exception as e:
        print(f"❌ 查询持仓异常：{str(e)}")
        return None


# ===============================
# 等待订单成交
# ===============================
def wait_order_filled(symbol, order_id, timeout=5):
    account = get_account()
    start = time.time()
    while time.time() - start < timeout:
        try:
            ts, recv_win = get_server_time()
            url = f"{account['BASE_URL']}/fapi/v1/order"
            params = {
                "symbol": symbol,
                "orderId": order_id,
                "timestamp": ts,
                "recvWindow": recv_win
            }
            qs = "&".join(f"{k}={v}" for k, v in params.items())
            sig = hmac.new(account["API_SECRET"].encode(), qs.encode(), hashlib.sha256).hexdigest()
            params["signature"] = sig

            r = requests.get(url, params=params, timeout=2)
            res = r.json()
            status = res.get("status")

            if status == "FILLED":
                print(f"✅ 订单{order_id}已完全成交")
                return True
            if status in ["CANCELED", "REJECTED", "EXPIRED"]:
                print(f"⚠️ 订单{order_id}状态：{status}")
                return False
            time.sleep(0.3)
        except Exception as e:
            print(f"❌ 查询订单状态异常：{str(e)}")
            time.sleep(0.3)

    try:
        print(f"⏱️ 订单{order_id}超时未成交，执行自动撤单")
        ts, recv_win = get_server_time()
        cancel_url = f"{account['BASE_URL']}/fapi/v1/order"
        data = {
            "symbol": symbol,
            "orderId": order_id,
            "timestamp": ts,
            "recvWindow": recv_win
        }
        qs = "&".join(f"{k}={v}" for k, v in data.items())
        sig = hmac.new(account["API_SECRET"].encode(), qs.encode(), hashlib.sha256).hexdigest()
        data["signature"] = sig
        requests.delete(cancel_url, data=data, timeout=2)
    except Exception as e:
        print(f"❌ 撤单异常：{str(e)}")
    return False