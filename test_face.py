#!/usr/bin/env python3
import os
import time
from luma.core.interface.serial import i2c
from luma.oled.device import sh1106
from PIL import Image

# 配置路径
IMAGE_PATH = os.path.expanduser("~/wukong_face.png")
STOP_OLED = "sudo systemctl stop wukong-oled.service"
START_OLED = "sudo systemctl start wukong-oled.service"

def test_display():
    print("--- 正在启动图片显示测试 ---")
    
    # 1. 先停掉占用屏幕的服务
    print("[1/3] 停止时间服务...")
    os.system(STOP_OLED)
    time.sleep(1)

    # 2. 尝试显示图片
    print(f"[2/3] 正在加载并显示图片: {IMAGE_PATH}")
    try:
        serial = i2c(port=1, address=0x3C)
        device = sh1106(serial)
        
        if os.path.exists(IMAGE_PATH):
            # 加载图片并转为黑白点阵
            img = Image.open(IMAGE_PATH).convert('1')
            # 自动调整大小以适应屏幕 (防止图片尺寸不对导致报错)
            img = img.resize((128, 64))
            device.display(img)
            print(">>> 图片已推送到屏幕，请观察！")
        else:
            print(f">>> 错误：在 {IMAGE_PATH} 没找到图片文件！请检查上传路径。")
            
        print(">>> 预览将持续 10 秒...")
        time.sleep(10)
        
    except Exception as e:
        print(f">>> 发生错误: {e}")
    
    # 3. 恢复环境
    print("[3/3] 测试结束，恢复时间显示...")
    os.system(START_OLED)
    print("--- 测试完成 ---")

if __name__ == "__main__":
    test_display()
