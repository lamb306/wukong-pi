#!/usr/bin/env python3
import os, time, datetime, asyncio, subprocess, json, pathlib
import edge_tts
from luma.core.interface.serial import i2c
from luma.core.render import canvas
from luma.oled.device import sh1106
from PIL import Image

# ================= 1. 硬件与路径配置 =================
PLAY_CARD = "hw:3,0"
VOICE = "zh-CN-YunxiNeural"
TTS_MP3 = "/tmp/remind_tts.mp3"
TTS_WAV = "/tmp/remind_tts.wav"

# 静态图片路径 (请确保文件已上传至此)
IMAGE_PATH = os.path.expanduser("~/wukong_face.png")

STOP_OLED = "sudo systemctl stop wukong-oled.service"
START_OLED = "sudo systemctl start wukong-oled.service"

# ================= 2. 提醒名单 =================
REMINDERS = {
    "12:00": "小主，午时已到！肚子不打鸣吗？快去吃饭，俺老孙给你守着行李！",
    "15:00": "歇会儿，歇会儿！喝口水，翻个筋斗云去散散心。",
    "18:30": "日落西山，该收工吃饭了，吃饱了才有力气降妖除魔！",
}

# ================= 3. 核心工具 =================
async def tts_and_play(text):
    """生成并播放语音"""
    try:
        await edge_tts.Communicate(text, VOICE).save(TTS_MP3)
        os.system(f'ffmpeg -y -loglevel error -i "{TTS_MP3}" -ac 2 -ar 48000 -f wav "{TTS_WAV}" > /dev/null 2>&1')
        os.system(f'aplay -D {PLAY_CARD} -q "{TTS_WAV}"')
    except Exception as e:
        print(f"提醒执行失败: {e}")

def display_face():
    """在 OLED 上显示悟空头像"""
    try:
        serial = i2c(port=1, address=0x3C)
        device = sh1106(serial)
        
        if os.path.exists(IMAGE_PATH):
            # 加载并转换为 1-bit 模式 (黑白点阵)
            logo = Image.open(IMAGE_PATH).convert('1')
            device.display(logo)
        else:
            # 如果找不到图片，显示文字兜底
            with canvas(device) as draw:
                draw.text((30, 25), "悟空提醒中...", fill="white")
    except Exception as e:
        print(f"OLED 显示错误: {e}")

def release_oled():
    """彻底释放 I2C 接口"""
    try:
        serial = i2c(port=1, address=0x3C)
        device = sh1106(serial)
        device.cleanup()
    except: pass

# ================= 4. 主循环 =================
def main():
    print(">>> 悟空静态图提醒服务已启动...")
    has_reminded_today = {}

    while True:
        now = datetime.datetime.now()
        current_time = now.strftime("%H:%M")
        today = now.strftime("%Y-%m-%d")

        if current_time in REMINDERS:
            if has_reminded_today.get(current_time) != today:
                msg = REMINDERS[current_time]
                print(f"[{current_time}] 触发提醒: {msg}")

                # 1. 抢占屏幕
                os.system(STOP_OLED)
                time.sleep(0.5)

                # 2. 显示图片并播报
                display_face()
                asyncio.run(tts_and_play(msg))
                
                # 3. 释放资源并恢复时钟
                release_oled()
                os.system(START_OLED)
                
                has_reminded_today[current_time] = today
        
        time.sleep(30)

if __name__ == "__main__":
    main()