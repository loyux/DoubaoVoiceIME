# Linux 版

Linux 版提供豆包、千问、微信输入法、百度输入法和讯飞五个云端识别引擎，以及基于 llama.cpp 的本地试用模式。可以用全局快捷键长按录音，或按一次开始、再按一次结束。识别结果可以回填到原窗口、留在剪贴板，也可以在设置窗口查看和手动复制。云端识别需要联网。

## 安装和运行

从 [Releases](https://github.com/yc-2018/DoubaoVoiceIME/releases) 下载并解压 `DoubaoVoice-linux.tar.gz`，或者使用仓库源码。以下命令在解压后的目录或仓库根目录执行。

Arch Linux（Hyprland、Sway）

```sh
sudo pacman -S python python-gobject python-cairo gtk3 portaudio opus wl-clipboard gtk-layer-shell libayatana-appindicator
```

Debian / Ubuntu（Wayland）

```sh
sudo apt install python3 python3-venv python3-gi python3-gi-cairo gir1.2-gtk-3.0 libportaudio2 libopus0
sudo apt install wl-clipboard gir1.2-gtk-layer-shell-0.1 gir1.2-ayatanaappindicator3-0.1
```

X11 安装 GTK、PortAudio 和 Opus 后，再安装 `xdotool` 和 `xbindkeys`。 `gtk-layer-shell` 用于 Wayland 录音浮层，Ayatana AppIndicator 用于托盘。

```sh
python3 -m venv --system-site-packages .venv-linux
.venv-linux/bin/python -m pip install -r requirements.txt
./doubao-voice --settings
```

设置窗口里的“开始录音”可用于试录。先在文本编辑器里用全局快捷键测试回填。若要粘贴到终端，可在设置中选择 `Ctrl+Shift+V`。若开启“识别后用豆包整理文字”，应用会先整理，再粘贴最终文本。麦克风默认只在录音时打开，可在设置中开启预热。

麦克风选“系统默认”时，应用会优先经 PipeWire 或 PulseAudio 录音，跟随桌面当前的默认输入。也可以在设置里指定一台麦克风。

应用会把结果留在剪贴板。Hyprland、Sway 和 X11 会检查前台窗口是否仍与录音开始时相同，再发送粘贴快捷键。其他 Wayland 桌面可使用“复制结果”或系统剪贴板。录音浮层在支持 layer-shell 的 Wayland 桌面及 X11 上可用。

提示框居中显示在屏幕底部，分为两行。上行显示状态和音量，下行用整行宽度显示转录。录音时显示红点，松开快捷键后显示“正在转文字…”和旋转图标。长句显示最新一段，完整结果可以在设置里查看。

## 全局快捷键

先启动程序，然后创建一个固定路径，供桌面的快捷键使用。也可以直接在配置里写启动脚本的绝对路径。

```sh
mkdir -p ~/.local/bin
ln -s "$PWD/doubao-voice" ~/.local/bin/doubao-voice
```

### Hyprland

在使用 Lua 配置的 Hyprland 版本中，把以下配置加到 `~/.config/hypr/hyprland.lua`。`F8` 按下开始、松开结束，`Shift+F8` 取消。快捷键可自行更换。

```lua
local doubao_voice = "~/.local/bin/doubao-voice"
hl.bind("F8", hl.dsp.exec_cmd(doubao_voice .. " --start"))
hl.bind("F8", hl.dsp.exec_cmd(doubao_voice .. " --stop"), { release = true })
hl.bind("SHIFT + F8", hl.dsp.exec_cmd(doubao_voice .. " --cancel"))
```

免按模式把上面两条 `F8` 绑定换成 `hl.bind("F8", hl.dsp.exec_cmd(doubao_voice .. " --toggle"))`。若需要由 Hyprland 启动程序，可以添加 `hl.on("hyprland.start", function() hl.exec_cmd(doubao_voice) end)`。

使用旧版 `hyprland.conf` 的 Hyprland 可使用以下语法。

```ini
bind = , F8, exec, ~/.local/bin/doubao-voice --start
bindr = , F8, exec, ~/.local/bin/doubao-voice --stop
bind = SHIFT, F8, exec, ~/.local/bin/doubao-voice --cancel
```

免按模式使用这一条，替换上面两条 `F8` 绑定。

```ini
bind = , F8, exec, ~/.local/bin/doubao-voice --toggle
```

可从设置启用 XDG 登录自启。若桌面未运行 XDG 自启程序，可用 Hyprland 的 `exec-once = ~/.local/bin/doubao-voice`。

### Sway

```ini
bindsym --no-repeat F8 exec ~/.local/bin/doubao-voice --start
bindsym --release F8 exec ~/.local/bin/doubao-voice --stop
bindsym --no-repeat Shift+F8 exec ~/.local/bin/doubao-voice --cancel
```

### X11 或其他桌面

桌面自定义快捷键里可把 `F8` 绑定为 `~/.local/bin/doubao-voice --toggle`。如果桌面支持分别绑定按下和松开事件，可以使用 `--start` 和 `--stop`。例如 X11 可把下面的配置加入 `~/.xbindkeysrc`，由 `xbindkeys` 加载。

```text
"~/.local/bin/doubao-voice --start"
    F8
"~/.local/bin/doubao-voice --stop"
    Release + F8
"~/.local/bin/doubao-voice --cancel"
    Shift + F8
```

## 命令行和数据目录

```sh
./doubao-voice --help
./doubao-voice --engine qwen --once --wav speech.wav
./doubao-voice --settings
./doubao-voice --quit
```

WAV 模式接受 16 kHz、16 bit、单声道 PCM，向终端输出识别文本，不需要图形会话。普通模式可用 `--device N` 指定麦克风，编号见设置窗口或 `.venv-linux/bin/python -m sounddevice`。

设置、设备标识和 `hotwords.txt` 默认在 `~/.config/doubao-voice/`，日志默认在 `~/.local/state/doubao-voice/logs/`。程序遵循 `XDG_CONFIG_HOME` 和 `XDG_STATE_HOME`。

一次粘贴可用目标应用的撤销功能撤回。

## F9 本地转录和整理

按住 F9 说话时，屏幕底部浮层逐步显示 Zipformer 的本地预览。松开后，Qwen3-ASR-0.6B 根据完整录音转录，Qwen3-1.7B 整理文字，结果在同一浮层更新并粘贴。转录和整理通过本机的 llama.cpp 运行，首次使用时加载模型，后续录音复用模型。

准备支持 Qwen3-ASR 音频分块修复的 `llama-server`。可以使用 PATH 里的二进制，或将 llama.cpp 的完整运行目录放到 `~/.local/share/doubao-voice/llama.cpp/`。模型默认读取 `~/.local/share/doubao-voice/models/`，也可以在设置窗口分别选择 GGUF。设置 `XDG_DATA_HOME` 时优先使用该目录下的 `doubao-voice/`。

| 文件 | 来源 |
| --- | --- |
| `Qwen3-ASR-0.6B-Q8_0.gguf` | [Qwen3-ASR GGUF](https://www.modelscope.cn/models/ggml-org/Qwen3-ASR-0.6B-GGUF/files) |
| `mmproj-Qwen3-ASR-0.6B-Q8_0.gguf` | [配套音频编码器](https://www.modelscope.cn/models/ggml-org/Qwen3-ASR-0.6B-GGUF/files) |
| `Qwen3-1.7B-Q4_K_M.gguf` | [Qwen3 文字整理模型](https://www.modelscope.cn/models/unsloth/Qwen3-1.7B-GGUF/files) |

启用边说边预览时，在应用使用的 Python 环境安装依赖。

```sh
.venv-linux/bin/python -m pip install sherpa-onnx numpy
```

从 [Zipformer 中英双语模型](https://huggingface.co/csukuangfj/sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20/tree/main) 下载 `encoder-epoch-99-avg-1.int8.onnx`、`decoder-epoch-99-avg-1.onnx`、`joiner-epoch-99-avg-1.int8.onnx` 和 `tokens.txt`，放到上述模型目录的 `zipformer-zh-en/` 子目录。全精度解码器可减少预览中的重字，已有 INT8 解码器的安装可以补上该文件。流式依赖或模型缺失时，仍可松键后转录和整理。

Hyprland 的 Lua 绑定可以沿用前文的启动脚本路径。

```lua
hl.bind("F9", hl.dsp.exec_cmd(doubao_voice .. " --start-local"))
hl.bind("F9", hl.dsp.exec_cmd(doubao_voice .. " --stop-local"), { release = true })
```

也可以从终端用 WAV 文件测试本地转录和整理。

```sh
./doubao-voice --engine qwen_local --once --wav speech.wav
```
