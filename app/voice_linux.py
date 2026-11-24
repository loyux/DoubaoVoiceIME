"""Linux command line, recording lifecycle and desktop control socket."""

import argparse
import errno
import hashlib
import os
import socket
import struct
import sys
import threading
import time
import traceback
from pathlib import Path

ACTIONS = ("start", "stop", "start-local", "stop-local", "toggle", "cancel", "settings", "quit")
LOCAL_ENGINE = "qwen_local"
CREDENTIALS = struct.Struct("3i")
MAX_RECORDING_SECONDS = 30


def control_address():
    runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    wayland = os.environ.get("WAYLAND_DISPLAY")
    display = str(Path(runtime) / wayland) if wayland else f'{runtime}/{os.getenv("DISPLAY", "")}'
    digest = hashlib.sha256(display.encode()).hexdigest()[:16]
    return f"\0doubao-voice-{os.getuid()}-{digest}"


def send_action(action):
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as client:
        client.sendto(action.encode("ascii"), control_address())


def open_control_socket():
    server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
        server.bind(control_address())
        server.setblocking(False)
    except OSError:
        server.close()
        raise
    return server


def receive_action(server):
    data, ancillary, flags, _ = server.recvmsg(32, socket.CMSG_SPACE(CREDENTIALS.size))
    if flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC):
        return None
    for level, kind, value in ancillary:
        if (level, kind) == (socket.SOL_SOCKET, socket.SCM_CREDENTIALS):
            _, uid, _ = CREDENTIALS.unpack(value)
            if uid == os.getuid():
                action = data.decode("ascii", errors="replace")
                return action if action in ACTIONS else None
    return None


def launcher_command():
    app_dir = Path(__file__).resolve().parent
    launcher = app_dir.parent / "doubao-voice"
    return [str(launcher)] if launcher.is_file() else [sys.executable, str(app_dir / "voice_ime.py")]


def new_session(config, verbose, on_text=None, cancelled=None):
    if config["asr_engine"] == LOCAL_ENGINE:
        from api_llama import LocalASRSession

        return LocalASRSession(config, cancelled if cancelled is not None else threading.Event(), on_text)
    from voice_core import create_cloud_engine

    return create_cloud_engine(config["asr_engine"]).new_session(verbose, on_text=on_text)


def recognize_wav(args, config):
    from voice_core import open_pcm_wav, push_wav, log

    session = None
    try:
        with open_pcm_wav(args.wav) as audio:
            session = new_session(config, args.verbose)
            session.start()
            push_wav(session, audio, pace=config["asr_engine"] != LOCAL_ENGINE)
            session.finish()
            result = finalize_text(session, config)
        if not result:
            print("未识别到内容", file=sys.stderr)
            return 1
        print(result)
        return 0
    except Exception as exc:
        log(traceback.format_exc())
        print(f"识别失败：{exc}", file=sys.stderr)
        return 1
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:
                log(traceback.format_exc())


def finalize_text(session, config, on_organizing=None):
    from voice_core import apply_hotwords, format_transcript, log, smart_organize

    original = format_transcript(session.text, config)
    if not original:
        error = getattr(session, "_error", None)
        if error is not None:
            raise RuntimeError(str(error))
    elif config.get("smart_text") is True and 5 <= len(original) <= 12000:
        if on_organizing is not None:
            on_organizing()
        local = config.get("asr_engine") == LOCAL_ENGINE
        if local:
            from api_llama import organize

            outcome, result = smart_organize(original, lambda text: organize(text, config))
        else:
            outcome, result = smart_organize(original)
        if outcome == "rewrite":
            return (format_transcript(result, config) if local else apply_hotwords(result)) or original
        if outcome == "error":
            log(f"smart organize failed: {result}")
    return original


class Recording:
    """Feed the microphone into one transcription session and gate short taps locally."""

    def __init__(self, mic, config, verbose, hold_ms, on_live, on_status, on_done):
        self.mic = mic
        self.config = config.copy()
        self.hold_seconds = hold_ms / 1000
        self.on_status = on_status
        self.on_done = on_done
        self.cancelled = threading.Event()
        self.session = new_session(config, verbose, on_text=on_live, cancelled=self.cancelled)
        self.finished = threading.Event()
        self.started = time.monotonic()

    def start(self):
        self.mic.open()
        self.mic.attach(self.session)
        threading.Thread(target=self._recognize, daemon=True).start()

    def stop(self, cancel=False):
        if cancel or (not self.finished.is_set()
                      and time.monotonic() - self.started < self.hold_seconds):
            self.cancelled.set()
        if not self.finished.is_set():
            self.mic.detach(self.session)
            if self.config.get("mic_mode") != "always":
                self.mic.close()
            self.session.request_release()
            self.finished.set()

    def _recognize(self):
        from voice_core import log

        result, error = "", ""
        try:
            # Allow the desktop to process a release queued while PortAudio was opening.
            self.finished.wait(self.hold_seconds)
            if not self.cancelled.is_set():
                self.session.start()
                self.finished.wait()
                if not self.cancelled.is_set():
                    self.session.finish()
                if not self.cancelled.is_set():
                    message = ("正在本地整理…" if self.config["asr_engine"] == LOCAL_ENGINE
                               else "正在整理文字…")
                    result = finalize_text(self.session, self.config,
                                           lambda: self.on_status(message))
        except Exception as exc:
            log(traceback.format_exc())
            error = str(exc)
        finally:
            try:
                if self.cancelled.is_set():
                    self.session.cancel()
                self.session.close()
            except Exception:
                log(traceback.format_exc())
            self.on_done(self, result, error)


def main():
    parser = argparse.ArgumentParser(description="豆包语音输入 · Linux")
    commands = parser.add_mutually_exclusive_group()
    for action, help_text in (
        ("start", "开始长按录音"), ("stop", "结束长按录音"), ("toggle", "开始或结束免按录音"),
        ("start-local", "开始本地录音"), ("stop-local", "结束本地录音并整理"),
        ("cancel", "取消录音"), ("settings", "打开设置"), ("quit", "退出正在运行的实例"),
    ):
        commands.add_argument(f"--{action}", action="store_const", dest="action",
                              const=action, help=help_text)
    parser.add_argument("--device", type=int, help="本次运行使用的麦克风编号")
    parser.add_argument("--wav", help="识别 16 kHz、16 bit、单声道 PCM WAV 文件")
    parser.add_argument("--once", action="store_true", help="识别 --wav 后输出到终端并退出")
    parser.add_argument("--no-inject", action="store_true", help="只显示结果")
    parser.add_argument("--no-ui", action="store_true", help="隐藏录音浮层")
    parser.add_argument("--no-tray", action="store_true", help="隐藏托盘图标")
    parser.add_argument("--verbose", action="store_true", help="记录识别中间结果")
    parser.add_argument("--engine", choices=("doubao_api", "qwen", "wetype", "baidu", "ifly", LOCAL_ENGINE),
                        help="本次运行使用的识别引擎")
    args = parser.parse_args()
    if bool(args.wav) != args.once or (args.once and args.action):
        parser.error("--once 和 --wav 必须一起使用，且不能与桌面控制命令组合")
    if args.action:
        try:
            send_action(args.action)
            return 0
        except OSError as exc:
            if args.action != "settings" or exc.errno != errno.ECONNREFUSED:
                print(f"无法连接桌面实例：{exc}", file=sys.stderr)
                return 1

    from voice_core import CLOUD_ENGINES, load_config, ensure_hotwords_file, log

    config = load_config()
    kind = args.engine or config.get("asr_engine")
    config["asr_engine"] = kind if isinstance(kind, str) and (
        kind in CLOUD_ENGINES or kind == LOCAL_ENGINE
    ) else "doubao_api"
    if config["asr_engine"] == LOCAL_ENGINE:
        config["smart_text"] = True
    if args.once:
        return recognize_wav(args, config)

    try:
        server = open_control_socket()
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            if args.action == "settings":
                try:
                    send_action("settings")
                    return 0
                except OSError:
                    pass
            print("豆包语音输入已在当前桌面运行，可用 --settings 打开设置。", file=sys.stderr)
        else:
            print(f"无法启动桌面控制接口：{exc}", file=sys.stderr)
        return 1

    with server:
        try:
            from linux_desktop import run
        except (ImportError, ValueError) as exc:
            print(f"无法加载 GTK 3，请安装 Python GObject 和 GTK 3：{exc}", file=sys.stderr)
            return 1
        ensure_hotwords_file()
        log("=== Linux desktop start")
        return run(args, config, server)
