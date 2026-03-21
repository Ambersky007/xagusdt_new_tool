import pyttsx3
import threading
import time
from queue import Queue
import re
import keyboard  # pip install keyboard

# ===============================
# 初始化语音引擎
# ===============================
engine = pyttsx3.init()
engine.setProperty('rate', 180)  # 语速
engine.setProperty('volume', 1.0)  # 音量

# ===============================
# 消息队列与节流管理
# ===============================
_speak_queue = Queue()  # 消息队列
_last_speak_time = {}   # 上次播报时间记录
_speak_interval = 20    # 普通消息节流（秒）

# ===============================
# 语音开关
# ===============================
voice_enabled = True

def toggle_voice(on: bool):
    """切换语音播报开关，每次切换提示一次"""
    global voice_enabled
    voice_enabled = on
    beep_text = "语音已开启" if on else "语音已关闭"
    _speak_queue.put((True, beep_text))  # force=True 强制播报

def _keyboard_listener():
    """监听键盘按键，K 开启，V 关闭"""
    while True:
        try:
            if keyboard.is_pressed('v'):
                toggle_voice(False)
                time.sleep(0.5)  # 防止连续触发
            elif keyboard.is_pressed('k'):
                toggle_voice(True)
                time.sleep(0.5)
        except:
            pass

threading.Thread(target=_keyboard_listener, daemon=True).start()

# ===============================
# 主播报函数
# ===============================
def speak(text, key=None, force=False):
    """
    异步播报语音
    text: 文本内容
    key: 节流 key
    force: True 表示强制播报，不受节流限制（下单消息优先）
    """
    global _last_speak_time, voice_enabled
    if not voice_enabled:
        return  # 关闭语音则不放入队列

    # 去掉 emoji 或特殊符号，只保留文字、数字和标点
    clean_text = re.sub(r'[^\u4e00-\u9fa5a-zA-Z0-9。，,.!?]', '', text)

    if key is None:
        key = clean_text

    now = time.time()
    last_time = _last_speak_time.get(key, 0)

    # 普通消息节流
    if not force and (now - last_time < _speak_interval):
        return

    _last_speak_time[key] = now
    _speak_queue.put((force, clean_text))

# ===============================
# 播报线程
# ===============================
def _speak_worker():
    while True:
        force, text = _speak_queue.get()
        engine.say(text)
        engine.runAndWait()
        _speak_queue.task_done()

# 启动后台播报线程（守护线程）
threading.Thread(target=_speak_worker, daemon=True).start()