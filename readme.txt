主函數的運行邏輯
循环开始 (每5秒)
│
├─► 检查交易锁 trade_lock
│       └─ 如果锁定 → 等待1秒 → 继续循环
│
├─► 检查 WS 状态 & HTTP兜底
│       └─ WS 异常或超时 → 调用 http_update()
│
├─► 获取最新价格
│       └─ 价格获取失败 → 调用 http_update()
│
├─► 读取 K 线数据 (1m/5m/15m/1h)
│
├─► 缓存持仓信息 (10秒/次)
│       └─ 获取失败 → 打印异常 → continue
│
├─► 持仓平仓检查
│       ├─ 如果有持仓：
│       │      ├─ 调用 should_exit_position()
│       │      │       ├─ 返回 True → **触发主动平仓下单** (place_order)
│       │      │       └─ 返回 False → 等待平仓信号，打印日志 → continue
│
├─► 指标计算 (ATR, MACD, 趋势, 剃头皮区间)
│
├─► 剃头皮信号判断
│       ├─ scalp_signal_up / scalp_signal_down
│
├─► 趋势信号判断
│       ├─ trend_long / trend_short
│       └─ MACD 金叉/死叉 + 低成交量 + 成交量扩张
│
├─► 最终信号判定
│       ├─ 优先剃头皮信号
│       └─ 无剃头皮 → 使用趋势信号
│
├─► 风控过滤
│       ├─ EMA20偏离 > 1%
│       ├─ ATR 风控
│       └─ 盘口价差过大
│
├─► 止盈止损计算
│
├─► 下单判断
│       ├─ 如果 entry_signal 属于多单信号 → **place_order 买入**
│       └─ 如果 entry_signal 属于空单信号 → **place_order 卖出**
│
└─► 日志打印