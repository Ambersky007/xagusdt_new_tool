# ===============================
# order_executor.py
# 下单功能模块（支持剃头皮/追单、止盈止损、滑点、订单管理）
# ===============================

import requests  # 用于发送HTTP请求
import time  # 时间戳与延时控制
import hmac  # HMAC签名
import hashlib  # SHA256
from config_account import TRADE_MODE, REAL_ACCOUNT, TEST_ACCOUNT  # 获取账户信息
from price_center import get_price
from config_account import (
    API_KEY, API_SECRET, SYMBOL,
    CURRENT_CONFIG, BASE_URL  # 导入BASE_URL和其他配置
)

# 全局订单列表，用于止盈止损监控
order_list = []

# 默认滑点设置（0.01‰）
DEFAULT_SLIPPAGE = 0.0001  # 这个滑点就是0.1usdt


def get_account():
    """
    根据 TRADE_MODE 返回当前使用的账户
    """
    return TEST_ACCOUNT if TRADE_MODE == "TEST" else REAL_ACCOUNT


def place_order(symbol, side, quantity, price=None, order_type="NORMAL",
                scalping_range=None, stop_loss=None, take_profit=None,
                slippage=DEFAULT_SLIPPAGE,
                real_order_type="MARKET",  # 默认市价
                reduce_only=False):
    """
    下单函数（支持 LIMIT 挂单开仓 / MARKET 市价平仓）
    """
    account = get_account()

    # -----------------------------
    # 剃头皮：LIMIT 挂单开仓（吃Maker）
    # -----------------------------
    if order_type == "SCALPING" and scalping_range:
        low, high = scalping_range
        if side == "BUY":
            price = low - 0.02  # 多单挂区间下沿
        elif side == "SELL":
            price = high + 0.02 # 空单挂区间上沿
        # ✅ 关键：剃头皮强制用 LIMIT 挂单，不吃市价
        real_order_type = "LIMIT"

    # -----------------------------
    # 滑点控制（只对 LIMIT 单有效）
    # -----------------------------
    # -----------------------------
    # 滑点控制（只对 LIMIT 单有效，固定1美分）
    # -----------------------------
    market_price = get_price()
    max_slippage = 0.01  # 固定1美分滑点
    if price is not None and real_order_type == "LIMIT":
        if side == "BUY" and price > market_price + max_slippage:
            print(f"⚠️ LIMIT买价超滑点：{price} > {market_price + max_slippage}")
            return None
        if side == "SELL" and price < market_price - max_slippage:
            print(f"⚠️ LIMIT卖价超滑点：{price} < {market_price - max_slippage}")
            return None

    # -----------------------------
    # 构造请求（LIMIT/MARKET 自动区分）
    # -----------------------------
    url = f"{account['BASE_URL']}/fapi/v1/order"
    headers = {"X-MBX-APIKEY": account["API_KEY"]}

    data = {
        "symbol": symbol,
        "side": side,
        "type": real_order_type,  # LIMIT / MARKET
        "quantity": quantity,
        "timestamp": int(time.time() * 1000),
        "reduceOnly": "true" if reduce_only else "false"
    }

    # ✅ LIMIT 单才需要 price + timeInForce；市价单不要加 price
    if real_order_type == "LIMIT" and price is not None:
        data["price"] = price
        data["timeInForce"] = "GTC"  # 挂单有效直到成交/撤销

    # 签名、发送...（下面完全不变）
    query_string = "&".join([f"{k}={v}" for k, v in data.items()])
    signature = hmac.new(account["API_SECRET"].encode(),
                         query_string.encode(), hashlib.sha256).hexdigest()
    data["signature"] = signature

    try:
        r = requests.post(url, headers=headers, data=data, timeout=5)
        res = r.json()
        print(f"💰 [{TRADE_MODE}] {real_order_type} 下单：{res}")
        # 记录订单...（完全不变）
        return res
    except Exception as e:
        print("❌ 下单失败:", e)
        return None
# 修复：添加 symbol 参数，适配主程序调用
def check_stop_orders(symbol):
    """
    遍历全局订单列表，触发止盈止损
    支持剃头皮止盈回落和平仓逻辑
    symbol: 交易对（如 XAGUSDT）
    """
    global order_list
    to_remove = []

    for order in order_list:
        # 只处理指定交易对的订单
        if order["symbol"] != symbol:
            continue

        side = order["side"]
        price = get_price()

        # 计算浮动盈利
        if side == "BUY":
            profit = price - order["price"] if price else 0
        else:
            profit = order["price"] - price if price else 0

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
                if profit >= 0.0005 * order["price"] and profit <= order["max_profit"] * 0.8:
                    print(f"🎯 向上剃头皮止盈回落, 平仓 {symbol}")
                    place_order(symbol, "SELL", order["qty"],
                                real_order_type="MARKET",
                                reduce_only=True)
                    to_remove.append(order)
                # 止损：价格 < 区间低点立即平仓
                elif price and price <= low:
                    print(f"⚠️ 向上剃头皮止损触发, 平仓 {symbol}")
                    place_order(symbol, "SELL", order["qty"],
                                real_order_type="MARKET",
                                reduce_only=True)
                    to_remove.append(order)
            # 向下剃头皮做空
            elif side == "SELL":
                # 止盈：盈利>=0.05%且回落到最大盈利的80%
                if profit >= 0.0005 * order["price"] and profit <= order["max_profit"] * 0.8:
                    print(f"🎯 向下剃头皮止盈回落, 平仓 {symbol}")
                    # 修复点1：空头平仓用 BUY
                    place_order(symbol, "BUY", order["qty"],
                                real_order_type="MARKET",
                                reduce_only=True)
                    to_remove.append(order)
                # 止损：价格 > 区间高点立即平仓
                elif price and price >= high:
                    print(f"⚠️ 向下剃头皮止损触发, 平仓 {symbol}")
                    # 修复点2：空头止损平仓用 BUY
                    place_order(symbol, "BUY", order["qty"],
                                real_order_type="MARKET",
                                reduce_only=True)
                    to_remove.append(order)

        # -----------------------------
        # 追单止盈/止损逻辑
        # -----------------------------
        if order["order_type"] == "NORMAL":
            # 多头追多
            if side == "BUY":
                if profit >= 0.0005 * order["price"] and profit <= order["max_profit"] * 0.8:
                    print(f"🎯 追多止盈回落, 平仓 {symbol}")
                    place_order(symbol, "SELL", order["qty"],
                                real_order_type="MARKET",
                                reduce_only=True)
                    to_remove.append(order)
                # 止损：价格 < 下单价 - 0.7
                elif price and price <= order["price"] - 0.7:
                    print(f"⚠️ 追多止损触发, 平仓 {symbol}")
                    place_order(symbol, "SELL", order["qty"],
                                real_order_type="MARKET",
                                reduce_only=True)
                    to_remove.append(order)
            # 空头追空
            elif side == "SELL":
                if profit >= 0.0005 * order["price"] and profit <= order["max_profit"] * 0.8:
                    print(f"🎯 追空止盈回落, 平仓 {symbol}")
                    # 修复点3：追空止盈平仓用 BUY
                    place_order(symbol, "BUY", order["qty"],
                                real_order_type="MARKET",
                                reduce_only=True)
                    to_remove.append(order)
                # 止损：价格 > 下单价 + 0.7
                elif price and price >= order["price"] + 0.7:
                    print(f"⚠️ 追空止损触发, 平仓 {symbol}")
                    # 修复点4：追空止损平仓用 BUY
                    place_order(symbol, "BUY", order["qty"],
                                real_order_type="MARKET",
                                reduce_only=True)
                    to_remove.append(order)

    # -----------------------------
    # 移除已触发的订单
    # -----------------------------
    for o in to_remove:
        order_list.remove(o)

    # 新增：打印检查结果
    if to_remove:
        print(f"ℹ️ {symbol} 本次触发 {len(to_remove)} 笔止盈止损单")
    else:
        print(f"ℹ️ {symbol} 暂无需要触发的止盈止损单")


# 优化：适配主程序的持仓判断逻辑，返回布尔值+详细信息
def get_position(symbol):
    """
    获取持仓方向
    return
        "LONG" / "SHORT" / None
    """
    try:
        endpoint = "/fapi/v2/positionRisk"
        timestamp = int(time.time() * 1000)

        query_string = f"timestamp={timestamp}"
        signature = hmac.new(
            API_SECRET.encode(),
            query_string.encode(),
            hashlib.sha256
        ).hexdigest()

        url = f"{BASE_URL}{endpoint}?{query_string}&signature={signature}"

        headers = {
            "X-MBX-APIKEY": API_KEY
        }

        response = requests.get(url, headers=headers, timeout=5)
        data = response.json()

        # API错误处理
        if isinstance(data, dict) and "code" in data:
            print(f"❌ 持仓查询API错误: {data.get('msg')}")
            return None

        for pos in data:
            if pos["symbol"] == symbol:
                amt = float(pos["positionAmt"])

                if amt > 0:
                    print(f"✅ {symbol} 多头持仓: {amt}")
                    return "LONG"
                elif amt < 0:
                    print(f"✅ {symbol} 空头持仓: {abs(amt)}")
                    return "SHORT"

        print(f"❌ {symbol} 无持仓")
        return None

    except Exception as e:
        print(f"❌ 查询持仓失败: {e}")
        return None


def wait_order_filled(symbol, order_id, timeout=5):
    """
    等待订单成交，超时自动撤单
    return: True=成交 / False=未成交/撤单
    """
    account = get_account()
    start = time.time()
    while time.time() - start < timeout:
        try:
            # 查询订单状态
            url = f"{account['BASE_URL']}/fapi/v1/order"
            params = {
                "symbol": symbol,
                "orderId": order_id,
                "timestamp": int(time.time()*1000)
            }
            qs = "&".join(f"{k}={v}" for k,v in params.items())
            sig = hmac.new(account["API_SECRET"].encode(),
                          qs.encode(), hashlib.sha256).hexdigest()
            params["signature"] = sig
            r = requests.get(url, params=params, timeout=2)
            res = r.json()
            status = res.get("status")

            if status == "FILLED":
                print(f"✅ 订单 {order_id} 已成交")
                return True
            if status in ["CANCELED", "REJECTED", "EXPIRED"]:
                print(f"⚠️ 订单 {order_id} 已{status}")
                return False

            time.sleep(0.3)
        except Exception as e:
            print(f"❌ 查询订单失败: {e}")
            time.sleep(0.3)

    # 超时：自动撤单
    try:
        print(f"⏱️ 订单 {order_id} 超时，自动撤单")
        cancel_url = f"{account['BASE_URL']}/fapi/v1/order"
        data = {
            "symbol": symbol,
            "orderId": order_id,
            "timestamp": int(time.time()*1000)
        }
        qs = "&".join(f"{k}={v}" for k,v in data.items())
        sig = hmac.new(account["API_SECRET"].encode(),
                      qs.encode(), hashlib.sha256).hexdigest()
        data["signature"] = sig
        requests.delete(cancel_url, data=data, timeout=2)
    except:
        pass
    return False