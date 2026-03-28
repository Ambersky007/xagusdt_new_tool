import pyttsx3
import threading
import time
from queue import Queue
import re
import keyboard

# ===============================
# 初始化语音引擎
# ===============================
engine = pyttsx3.init()
engine.setProperty('rate', 180)
engine.setProperty('volume', 1.0)

# ===============================
# 消息队列与节流管理
# ===============================
_speak_queue = Queue()
_last_speak_time = {}
_speak_interval = 20

# ===============================
# 语音总开关
# ===============================
voice_enabled = True

def toggle_voice(on: bool):
    """切换语音 + 提示播报"""
    global voice_enabled
    voice_enabled = on
    tip = "语音已开启" if on else "语音已关闭"
    _speak_queue.put((True, tip))   # 强制提示

# ===============================
# 键盘监听线程（还原你原来 K/V 控制）
# ===============================
def _keyboard_listener():
    while True:
        try:
            if keyboard.is_pressed('k'):
                toggle_voice(True)
                time.sleep(0.5)
            elif keyboard.is_pressed('v'):
                toggle_voice(False)
                time.sleep(0.5)
        except:
            time.sleep(0.2)

threading.Thread(target=_keyboard_listener, daemon=True).start()

# ===============================
# 后台播报工作线程（修复卡死/不发声核心）
# ===============================
def _speak_worker():
    global engine
    while True:
        try:
            force, text = _speak_queue.get()
            if not text.strip():
                _speak_queue.task_done()
                continue

            # 每次播报重建引擎，彻底解决pyttsx3长期不说话
            engine = pyttsx3.init()
            engine.setProperty('rate', 180)
            engine.setProperty('volume', 1.0)

            engine.say(text)
            engine.runAndWait()
            time.sleep(0.15)
            _speak_queue.task_done()
        except Exception as e:
            time.sleep(0.2)

# 启动播报线程
threading.Thread(target=_speak_worker, daemon=True).start()

# ===============================
# 对外播报接口（完全兼容你旧调用）
# ===============================
def speak(text, key=None, force=False):
    """
    text:播报内容
    key:节流标识
    force:True=无视冷却强制播报（下单/平仓用）
    """
    global _last_speak_time, voice_enabled
    if not voice_enabled:
        return

    # 过滤emoji和特殊符号
    clean_text = re.sub(r'[^\u4e00-\u9fa5a-zA-Z0-9。，,.!?：；]', '', text)
    if not clean_text:
        return

    if key is None:
        key = clean_text

    now = time.time()
    last_t = _last_speak_time.get(key, 0)

    # 非强制消息走节流
    if not force and (now - last_t < _speak_interval):
        return

    _last_speak_time[key] = now
    _speak_queue.put((force, clean_text))