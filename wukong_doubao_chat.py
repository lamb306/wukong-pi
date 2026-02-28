#!/usr/bin/env python3
import os, json, wave, time, subprocess, asyncio, urllib.request, urllib.error, pathlib
import webrtcvad
import edge_tts
import struct, math
import signal
import threading

# --- OLED 相关库 ---
from luma.core.interface.serial import i2c
from luma.core.render import canvas
from luma.oled.device import sh1106
from PIL import ImageFont

# ================= 1. 基础音频与硬件参数 =================
RATE = 16000
SAMPLE_WIDTH = 2
FRAME_MS = 30
FRAME_BYTES = int(RATE * FRAME_MS / 1000 * SAMPLE_WIDTH)

PIN_ID = "17"  
REC_CARD = "plughw:2,0"  # 强制锁定录音硬件
PLAY_CARD = "plughw:3,0"

# ================= 2. 路径配置 =================
WHISPER = os.path.expanduser("~/whisper.cpp/build/bin/whisper-cli")
WHISPER_MODEL = os.path.expanduser("~/whisper.cpp/models/ggml-tiny.bin")
OUT_WAV = os.path.expanduser("~/auto_stop.wav")
PROMPT_FILE = pathlib.Path.home() / "wukong_prompt.txt"

VOICE = "zh-CN-YunxiNeural"
TTS_MP3 = "/tmp/wukong_tts.mp3"
TTS_WAV = "/tmp/wukong_tts.wav"

VAD_LEVEL = 3
START_VOICED_FRAMES = 8
END_SILENCE_MS = 1500  # 沉默 1.5 秒即认为说完
MAX_RECORD_S = 10      # 最长录制 10 秒
RMS_THRESHOLD = 200

# ================= 3. 豆包 API 配置 =================
DOUBAO_API_KEY = "f70ce81b-ffc4-43cd-a44a-766c8853a820"
DOUBAO_ENDPOINT = "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
DOUBAO_MODEL = "doubao-seed-1-6-flash-250828"

# ================= 全局状态与线程锁 =================
EXIT_FLAG = False
WAKE_EVENT = threading.Event()

# ================= 4. 按键监听线程 =================
def button_monitor():
    global EXIT_FLAG
    is_pressed = False
    pressed_time = 0
    
    while True:
        res = subprocess.run(["pinctrl", "get", PIN_ID], capture_output=True, text=True)
        currently_pressed = "lo" in res.stdout
        
        if currently_pressed:
            if not is_pressed:
                is_pressed = True
                pressed_time = time.time()
            else:
                if time.time() - pressed_time >= 2.0:
                    if not EXIT_FLAG:
                        print("\n[按钮] 检测到长按，正在强行中断并退出...")
                        EXIT_FLAG = True 
        else:
            if is_pressed:
                held_for = time.time() - pressed_time
                is_pressed = False
                if held_for < 2.0 and not WAKE_EVENT.is_set():
                    print("\n[按钮] 短按触发，唤醒悟空！")
                    WAKE_EVENT.set()
        time.sleep(0.1)

# ================= 5. OLED 显示控制 =================
def oled_service_control(action):
    os.system(f"sudo systemctl {action} wukong-oled.service")

def clear_oled():
    try:
        serial = i2c(port=1, address=0x3C)
        device = sh1106(serial)
        device.clear()
        device.cleanup()
    except: pass

def show_on_screen_instant(title, text):
    """【新增】瞬间显示，绝对不阻塞录音和思考进程"""
    try:
        serial = i2c(port=1, address=0x3C)
        device = sh1106(serial)
        font = ImageFont.truetype("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", 14)
        with canvas(device) as draw:
            draw.text((0, 0), f"[{title}]", font=font, fill="white")
            lines = [text[i:i+9] for i in range(0, len(text), 9)]
            for i, line in enumerate(lines[:3]):
                draw.text((0, 18 + i*15), line, font=font, fill="white")
    except: pass

def play_and_show_sync(title, text, wav_path):
    """声音与画面同步分页滚动"""
    global EXIT_FLAG
    
    p = subprocess.Popen(["aplay", "-D", PLAY_CARD, "-q", wav_path])
    
    try:
        with wave.open(wav_path, 'rb') as wf:
            duration = wf.getnframes() / float(wf.getframerate())
    except: duration = 3.0
    
    lines = [text[i:i+9] for i in range(0, len(text), 9)]
    pages = [lines[i:i+3] for i in range(0, len(lines), 3)]
    
    if not pages:
        p.wait()
        return
        
    time_per_page = duration / len(pages)
    
    try:
        serial = i2c(port=1, address=0x3C)
        device = sh1106(serial)
        font = ImageFont.truetype("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", 14)
        
        for page in pages:
            if EXIT_FLAG:
                p.terminate()
                break
            with canvas(device) as draw:
                draw.text((0, 0), f"[{title}]", font=font, fill="white")
                for i, line in enumerate(page):
                    draw.text((0, 18 + i*15), line, font=font, fill="white")
            
            start_w = time.time()
            while time.time() - start_w < time_per_page:
                if EXIT_FLAG:
                    p.terminate()
                    return
                time.sleep(0.1)
    except: pass
    finally:
        while p.poll() is None:
            if EXIT_FLAG:
                p.terminate()
                break
            time.sleep(0.1)

# ================= 6. 核心功能 =================
def rms16le(pcm: bytes) -> float:
    n = len(pcm) // 2
    if n == 0: return 0.0
    samples = struct.unpack("<%dh" % n, pcm)
    return math.sqrt(sum(x * x for x in samples) / n)

def load_system_prompt():
    if PROMPT_FILE.exists():
        return PROMPT_FILE.read_text(encoding="utf-8")
    return "你叫悟空，是一个幽默风趣的机器人助手。"

def record_one_utterance() -> str:
    global EXIT_FLAG
    vad = webrtcvad.Vad(VAD_LEVEL)
    
    # 使用无延迟显示，屏幕秒变
    show_on_screen_instant("状态", "悟空正在倾听...")
    
    # 【修复核心】换回绝对可靠的 arecord
    cmd = ["arecord", "-D", REC_CARD, "-f", "S16_LE", "-r", "16000", "-t", "raw", "-q", "--buffer-size=32000"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    
    ring, speech = [], bytearray()
    voiced, silence_ms = 0, 0
    in_speech = False
    start_time = time.time()

    try:
        while time.time() - start_time < MAX_RECORD_S:
            if EXIT_FLAG: break 
            
            chunk = p.stdout.read(FRAME_BYTES)
            if not chunk or len(chunk) < FRAME_BYTES:
                if p.poll() is not None: break # 防止进程卡死
                continue

            rms = rms16le(chunk)
            is_speech = vad.is_speech(chunk, RATE) and (rms >= RMS_THRESHOLD)

            if not in_speech:
                if is_speech:
                    voiced += 1
                    if voiced >= START_VOICED_FRAMES:
                        in_speech = True
                        for b in ring: speech.extend(b)
                        ring.clear()
                        speech.extend(chunk)
                else:
                    voiced = 0
                    ring.append(chunk)
                    if len(ring) > 20: ring.pop(0)
            else:
                speech.extend(chunk)
                if is_speech: silence_ms = 0
                else:
                    silence_ms += FRAME_MS
                    if silence_ms >= END_SILENCE_MS: break
    finally:
        p.terminate()

    if len(speech) < int(RATE * SAMPLE_WIDTH * 1) or EXIT_FLAG: 
        return ""
    
    with wave.open(OUT_WAV, "wb") as wf:
        wf.setnchannels(1); wf.setsampwidth(SAMPLE_WIDTH); wf.setframerate(RATE)
        wf.writeframes(speech)
    return OUT_WAV

def whisper_transcribe(wav):
    try:
        out = subprocess.check_output(
            [WHISPER, "-m", WHISPER_MODEL, "-f", wav, "-l", "zh", "-nt", "--beam-size", "1"],
            text=True, stderr=subprocess.DEVNULL
        )
        lines = [l.strip() for l in out.splitlines() if l.strip()]
        return lines[-1] if lines else ""
    except: return ""

def doubao_chat(messages):
    data = json.dumps({
        "model": DOUBAO_MODEL, "messages": messages, "temperature": 0.8, "max_tokens": 200
    }, ensure_ascii=False).encode("utf-8")
    
    req = urllib.request.Request(DOUBAO_ENDPOINT, data=data, headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {DOUBAO_API_KEY}"
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            j = json.loads(r.read().decode("utf-8"))
            return j["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f">>> 豆包请求异常: {e}")
        return None

async def tts_generate_only(text, out_wav):
    await edge_tts.Communicate(text, VOICE).save(TTS_MP3)
    os.system(f'ffmpeg -y -loglevel error -i "{TTS_MP3}" -ac 2 -ar 48000 -f wav "{out_wav}" > /dev/null 2>&1')

# ================= 7. 对话控制流 =================
def start_conversation():
    global EXIT_FLAG
    EXIT_FLAG = False 
    
    oled_service_control("stop")
    time.sleep(0.2) 
    chat_history = [{"role": "system", "content": load_system_prompt()}]
    
    try:
        asyncio.run(tts_generate_only("俺老孙来也！", TTS_WAV))
        play_and_show_sync("悟空", "俺老孙来也！", TTS_WAV)
        
        while not EXIT_FLAG:
            wav = record_one_utterance()
            if EXIT_FLAG: break
            
            if not wav: 
                print(">>> 检测到环境安静，退出对话。")
                show_on_screen_instant("状态", "没听见声音，退下了")
                time.sleep(1)
                break 
            
            show_on_screen_instant("状态", "悟空正在思考...")
            text = whisper_transcribe(wav)
            if EXIT_FLAG: break
            if not text: 
                continue # 如果是咳嗽等杂音，不退出，继续下一轮倾听
            
            print(f">>> 你说: {text}")
            show_on_screen_instant("你说", text)
            
            chat_history.append({"role": "user", "content": text})
            if len(chat_history) > 7: chat_history = [chat_history[0]] + chat_history[-6:]
            
            reply = doubao_chat(chat_history)
            if EXIT_FLAG: break
            
            if reply:
                print(f">>> 悟空: {reply}")
                chat_history.append({"role": "assistant", "content": reply})
                
                asyncio.run(tts_generate_only(reply, TTS_WAV))
                if EXIT_FLAG: break
                play_and_show_sync("悟空", reply, TTS_WAV)
            else:
                break
    except Exception as e:
        print(f">>> 对话发生错误: {e}")
    finally:
        print(">>> 对话结束，清理屏幕并恢复时间服务...")
        EXIT_FLAG = False
        clear_oled()
        time.sleep(0.3)
        oled_service_control("start")

# ================= 8. 主进程 =================
def main():
    print(f"悟空后台服务启动 (PID: {os.getpid()})")
    os.system(f"pinctrl set {PIN_ID} ip pu")
    
    threading.Thread(target=button_monitor, daemon=True).start()
    
    clear_oled()
    oled_service_control("start")
    
    try:
        while True:
            WAKE_EVENT.wait()
            WAKE_EVENT.clear()
            start_conversation()
    except KeyboardInterrupt:
        pass
    finally:
        clear_oled()
        oled_service_control("start")

if __name__ == "__main__":
    main()
