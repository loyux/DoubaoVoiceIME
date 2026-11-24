"""Shared recording, cloud engines and text settings."""

import json
import os
import re
import struct
import threading
import time
import traceback
import wave
from pathlib import Path

import api_baidu
import api_common
import api_doubao
import api_ifly
import api_qwen
import api_smart
import api_wetype

APP_DIR = api_common.APP_DIR
state_home = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state"
LOG_DIR = str(Path(state_home) / "doubao-voice" / "logs")

CONFIG_PATH = os.path.join(APP_DIR, "config.json")
HOTWORDS_PATH = os.path.join(APP_DIR, "hotwords.txt")
HOTWORDS_HEADER = ("# 热词替换：每行一条，格式：原文 -> 替换为\n"
                   "# 以 # 开头的行会被忽略，修改后下次识别自动生效\n")
APP_RELEASE = "2.2.5"
_log_lock = threading.Lock()

CLOUD_ENGINES = {
    "doubao_api": ("豆包（云端 API）", api_doubao.DoubaoApiEngine),
    "qwen": ("千问（云端 API）", api_qwen.QwenEngine),
    "wetype": ("微信输入法（云端 API）", api_wetype.WeChatEngine),
    "baidu": ("百度输入法（云端 API）", api_baidu.BaiduEngine),
    "ifly": ("讯飞输入法（云端 API）", api_ifly.IflyEngine),
}


def create_cloud_engine(kind):
    return CLOUD_ENGINES[kind][1]()


def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    try:
        os.makedirs(APP_DIR, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except OSError:
        log("save config failed")


def ensure_hotwords_file():
    if os.path.exists(HOTWORDS_PATH):
        return
    try:
        os.makedirs(APP_DIR, exist_ok=True)
        with open(HOTWORDS_PATH, "w", encoding="utf-8") as f:
            f.write(HOTWORDS_HEADER)
    except OSError:
        log("create hotwords failed")


def load_hotwords():
    try:
        with open(HOTWORDS_PATH, "r", encoding="utf-8-sig") as f:
            lines = f.read().splitlines()
    except (OSError, UnicodeError):
        return []
    rules = {}
    for line in lines:
        item = line.strip()
        if not item or item.startswith("#"):
            continue
        item = item.replace("→", "->").replace("=>", "->")
        if "->" not in item:
            continue
        src, dst = item.split("->", 1)
        src, dst = src.strip(), dst.strip()
        if not src or not dst:
            continue
        rules[src] = dst
    return sorted(rules.items(), key=lambda pair: len(pair[0]), reverse=True)


def apply_hotwords(text, rules=None):
    rules = load_hotwords() if rules is None else rules
    if not text or not rules:
        return text
    mapping = dict(rules)
    pattern = re.compile("|".join(re.escape(src) for src, _ in rules))
    return pattern.sub(lambda m: mapping[m.group(0)], text)


MIN_HOLD_MS = (0, 150, 250, 400)
MIN_HOLD_DEFAULT = 250
_SENT_CHUNK_RE = re.compile(r".+?(?:[。！？!?；;]+[”’」』）】》〉]*|$)")
_REPLACE_PUNCT = "，。！？；：、…—～"
_TRAIL_PUNCT_RE = re.compile(r"[，。！？；：、,.!?;:…—～]+([”’」』）】》〉]*)$")
_TRAIL_PERIOD_RE = re.compile(r"[。．.]+([”’」』）】》〉]*)$")


def normalize_paragraphs(text, mode):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if mode not in ("blank", "single"):
        return text
    parts = []
    for line in text.split("\n"):
        for piece in _SENT_CHUNK_RE.findall(line.strip()):
            piece = piece.strip()
            if piece:
                parts.append(piece)
    if not parts:
        return ""
    return ("\n\n" if mode == "blank" else "\n").join(parts)


def format_text(text, punctuation="keep_all", paragraph="none"):
    text = (text or "").strip()
    if not text:
        return ""
    text = normalize_paragraphs(text, paragraph)
    if punctuation == "remove_period":
        text = "\n".join(_TRAIL_PERIOD_RE.sub(r"\1", line.rstrip())
                         for line in text.split("\n"))
    elif punctuation == "remove_trailing":
        text = "\n".join(_TRAIL_PUNCT_RE.sub(r"\1", line.rstrip())
                         for line in text.split("\n"))
    elif punctuation == "space_replace":
        text = text.translate({ord(ch): " " for ch in _REPLACE_PUNCT})
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r" ([”’」』）】》〉])", r"\1", text)
        text = re.sub(r"([“‘「『（【《〈]) ", r"\1", text)
        text = "\n".join(line.strip() for line in text.split("\n"))
        text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def smart_organize(text):
    try:
        content, no_rewrite = api_smart.organize(text)
    except Exception as exc:
        return "error", str(exc)
    if no_rewrite:
        return "same", ""
    content = content.replace("\r\r\n", "\n").replace("\r\n", "\n").replace("\r", "\n")
    content = re.sub(r"\n{3,}", "\n\n", content).strip()
    return ("rewrite", content) if content else ("same", "")


def log_path(now=None):
    stamp = now if now is not None else time.localtime()
    return os.path.join(LOG_DIR, time.strftime("voice_ime-%Y-%m-%d.log", stamp))


def log(msg):
    now = time.localtime()
    line = time.strftime("%H:%M:%S ", now) + str(msg)
    try:
        with _log_lock:
            os.makedirs(LOG_DIR, exist_ok=True)
            with open(log_path(now), "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except OSError:
        pass


api_common.logger = log


class Mic:
    def __init__(self, device=None):
        self.device = device
        self.stream = None
        self.sink = None
        self.lock = threading.Lock()
        self.pre = bytearray()
        self.level = 0.0

    def open(self):
        import sounddevice as sd
        if self.stream is not None:
            return
        candidates = [self.device]
        if self.device is None:
            # ALSA's default PCM can point at a hardware card independently of the desktop default.
            inputs = {device["name"].casefold(): index
                      for index, device in enumerate(sd.query_devices())
                      if device["max_input_channels"] > 0}
            candidates[:0] = [inputs[name] for name in ("pipewire", "pulse") if name in inputs]
        for device in candidates:
            try:
                self._open_stream(sd, device)
            except sd.PortAudioError as exc:
                if device == self.device:
                    raise
                log(f"desktop mic backend unavailable: {sd.query_devices(device)['name']}: {exc}")
            else:
                log(f"mic opened: {sd.query_devices(device, kind='input')['name']}")
                return

    def _open_stream(self, sd, device):
        stream = sd.RawInputStream(samplerate=16000, channels=1, dtype="int16",
                                   device=device, blocksize=0,
                                   callback=self._cb)
        with self.lock:
            self.pre.clear()
        try:
            stream.start()
        except Exception:
            try:
                stream.close()
            except Exception:
                pass
            raise
        self.stream = stream

    def _cb(self, indata, frames, t, status):
        try:
            b = bytes(indata)
        except Exception:
            return
        n = len(b) // 2
        if n:
            samples = struct.unpack("<%dh" % n, b[:n * 2])
            self.level = max(max(samples), -min(samples)) / 32768.0
        with self.lock:
            self.pre += b
            if len(self.pre) > 4800:
                del self.pre[:len(self.pre) - 4800]
            if self.sink is not None:
                self.sink.push(b)

    def attach(self, sess):
        with self.lock:
            sess.push(bytes(self.pre))
            self.sink = sess

    def detach(self, sess):
        with self.lock:
            if self.sink is sess:
                self.sink = None

    def close(self):
        stream = self.stream
        self.stream = None
        self.level = 0.0
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
            log("mic closed")
        with self.lock:
            del self.pre[:]


def list_input_devices():
    try:
        import sounddevice as sd
        devs = sd.query_devices()
        hostapis = sd.query_hostapis()
        out = []
        for i, d in enumerate(devs):
            if d.get("max_input_channels", 0) <= 0:
                continue
            hname = hostapis[d["hostapi"]]["name"]
            out.append({"index": i, "name": d.get("name") or f"设备 {i}",
                        "hostapi": hname})
        return out
    except Exception:
        log("list devices error " + traceback.format_exc().replace("\n", " | "))
        return []


def resolve_mic_device(sel):
    if not isinstance(sel, dict) or not sel.get("name"):
        return None
    try:
        import sounddevice as sd
        name, hname = sel["name"], sel.get("hostapi")
        devs = sd.query_devices()
        hostapis = sd.query_hostapis()
        for i, d in enumerate(devs):
            if (d.get("max_input_channels", 0) > 0 and d.get("name") == name
                    and (not hname or hostapis[d["hostapi"]]["name"] == hname)):
                return i
        for i, d in enumerate(devs):
            if d.get("max_input_channels", 0) > 0 and d.get("name") == name:
                return i
    except Exception:
        log("resolve device error " + traceback.format_exc().replace("\n", " | "))
    return None


def open_pcm_wav(path):
    audio = wave.open(path, "rb")
    if (audio.getnchannels(), audio.getframerate(), audio.getsampwidth()) != (1, 16000, 2):
        audio.close()
        raise ValueError("WAV 必须是 16 kHz、16 bit、单声道 PCM")
    return audio


def push_wav(sess, audio):
    deadline = time.monotonic()
    while not sess.cancelled and (data := audio.readframes(640)):
        sess.push(data)
        deadline += len(data) / 32000
        time.sleep(max(0, deadline - time.monotonic()))
    sess.request_release()
