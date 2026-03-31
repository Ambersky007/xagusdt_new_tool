# ===============================
# time_sync.py
# 时间同步模块（解决Binance时间偏差问题）
# ===============================

import time
import requests

# 全局时间偏移（毫秒）
time_offset = 0


# ===============================
# 获取服务器时间
# ===============================
def get_server_time():
    url = "https://fapi.binance.com/fapi/v1/time"
    return requests.get(url, timeout=5).json()["serverTime"]


# ===============================
# 同步时间偏移
# ===============================
def sync_time():
    global time_offset
    try:
        url = "https://fapi.binance.com/fapi/v1/time"
        server_time = int(requests.get(url, timeout=3).json()["serverTime"])
        local_time = int(time.time() * 1000)

        # 计算偏移（关键）
        time_offset = server_time - local_time
        print(f"🕒 时间同步完成，偏移: {time_offset} ms")
    except Exception as e:
        print(f"❌ 时间同步失败: {e}")


# ===============================
# 获取修正后的时间戳（核心函数）
# ===============================
def get_timestamp():
    return int(time.time() * 1000) + time_offset