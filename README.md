# 豆包语音输入 · Linux

在 Linux 桌面上按住快捷键说话，松开后把识别结果粘贴到当前输入框。也可以按一次开始、再按一次结束。屏幕底部的浮层会显示录音和转文字状态。

- 支持豆包、千问、微信输入法、百度输入法和讯飞五种云端识别引擎。
- 可绑定 F9 试用本地流式预览，松键后由 Qwen3-ASR 转录并在同一浮层显示本地整理结果。
- 支持 Hyprland、Sway 和 X11 的快捷键和自动粘贴，切换窗口时将结果留在剪贴板。
- 提供托盘、图形设置、热词替换、文字整理和 WAV 命令行识别。

安装依赖后，从仓库或解压后的发行包目录运行：

```sh
./doubao-voice --settings
```

[安装与桌面快捷键配置](docs/linux.md) · [第三方组件声明](THIRD_PARTY_NOTICES.md) · [发行包](https://github.com/yc-2018/DoubaoVoiceIME/releases)

云端模式把录音发送给选中的识别引擎，开启文字整理时还会调用豆包文字整理服务。F9 本地模式在设备上完成转录和整理。配置、热词和设备标识默认保存在 `~/.config/doubao-voice/`。

## 致谢

云端识别引擎的协议细节与实现思路参考了 [J3n5en/voicekey](https://github.com/J3n5en/voicekey)。
