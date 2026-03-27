import requests
import time

class US10YSpider:
    def __init__(self):
        self.url = "https://api.investing.com/api/financialdata/650/historical/chart/?interval=PT1M"

        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Accept": "application/json",
            "Referer": "https://www.investing.com/",
            "Origin": "https://www.investing.com"
        }

        self.last_value = None

    def fetch(self):
        try:
            res = requests.get(self.url, headers=self.headers, timeout=5)

            if res.status_code != 200:
                print("状态码异常:", res.status_code)
                return self.last_value

            # 👉 防止不是json
            if "application/json" not in res.headers.get("Content-Type", ""):
                print("被反爬拦截")
                return self.last_value

            data = res.json()

            value = data["data"][-1]["last"]

            self.last_value = value
            return value

        except Exception as e:
            print("获取失败:", e)
            return self.last_value


if __name__ == "__main__":
    spider = US10YSpider()

    while True:
        print("10Y:", spider.fetch())
        time.sleep(5)