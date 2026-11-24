# 豆包语音输入 · Linux

在 Linux 桌面上按住快捷键说话，松开后把识别结果粘贴到当前输入框。也可以按一次开始、再按一次结束。屏幕底部的浮层会显示录音和转文字状态。

- 支持豆包、千问、微信输入法、百度输入法和讯飞五种云端识别引擎。
- 支持 Hyprland、Sway 和 X11 的快捷键和自动粘贴，切换窗口时将结果留在剪贴板。
- 提供托盘、图形设置、热词替换、文字整理和 WAV 命令行识别。

安装依赖后，从仓库或解压后的发行包目录运行：

```sh
./doubao-voice --settings
```

[安装与桌面快捷键配置](docs/linux.md) · [第三方组件声明](THIRD_PARTY_NOTICES.md) · [发行包](https://github.com/yc-2018/DoubaoVoiceIME/releases)

录音会发送给选中的识别引擎。开启文字整理时，识别出的文字会额外发送至豆包文字整理服务。配置、热词和设备标识默认保存在 `~/.config/doubao-voice/`。

## 致谢

云端识别引擎的协议细节与实现思路参考了 [J3n5en/voicekey](https://github.com/J3n5en/voicekey)。
