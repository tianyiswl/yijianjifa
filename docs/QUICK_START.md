# 从源码开始使用

本仓库公开 0.1.42 源码，软件与系统配音免费。当前没有新版免费 Windows/Mac 程序包下载链接；GitHub 自动生成的源码 ZIP 不是 Windows 便携程序。

## 准备环境

1. 安装 Python 3.12，并下载或克隆[本仓库](https://github.com/tianyiswl/yijianjifa)。
2. 安装 FFmpeg 和 ffprobe，将它们加入 PATH。分别运行 `ffmpeg -version`、`ffprobe -version` 确认可执行。视频字幕和文字渲染需要相应 FFmpeg 构建包含 `subtitles`、`drawtext` 等功能。开发依赖中的 `imageio-ffmpeg` 只提供 FFmpeg，不能替代 ffprobe。
3. 在仓库根目录创建虚拟环境，按操作系统安装依赖。以下首次启动使用独立数据目录，不读取其他版本的日常资料。

### Windows PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e '.[dev,publish]'
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe -m yijianjifa.webui.windows_app --data-dir "$env:LOCALAPPDATA\YiJianJiFa-Source-Test"
```

### macOS

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[dev,publish,mac-webui]'
.venv/bin/python -m playwright install chromium
.venv/bin/python -m yijianjifa.webui.mac_app --data-dir "$HOME/Library/Application Support/YiJianJiFa-Source-Test"
```

系统配音调用本机已有的系统声音。若系统没有需要的语言声音，先通过系统设置安装对应声音。Linux 不是本次桌面交付的验收目标。

`--data-dir` 可换成其他新的绝对路径；继续使用同一目录会保留该目录中的资料和账号记录。请勿把个人资料复制进仓库。启动配置细节见[配置说明](CONFIGURATION.md)。

## 制作并发布

1. 在“资料库”导入自己拍摄的视频，添加或导入自己的文案。
2. 在“账号管理”添加并登录需要发布的平台账号。支持抖音、快手、视频号和 B 站，同一平台可以添加多个账号。
3. 回到“混剪制作”，选择素材、文案和账号，按需设置配音、字幕、音乐与画面。
4. 设置开始日期、发布天数、每天条数、首条时间及时间间隔。
5. 点击“一键制作并发布”。软件先制作视频，再向平台提交发布安排；也可“只生成视频”后预览和导出。
6. 到“任务记录”查看结果。需要平台登录或短信验证时按提示处理；结果待确认时先核对原结果，避免重复投稿。

## 配音与可选识别

默认使用免费的系统配音，不需要注册云配音账号或充值。

云配音默认未连接服务。主动配置服务后才可登录、选择云音色并生成；文字试听与制作都受该服务字数和计费规则约束。本地音频长期保存，复用或重新下载原音频不扣字数；确认原音频无法恢复而需重新生成时，按成功字数扣费。网络暂时异常或原结果未知不会直接重生成。

官方音色示例 MP3 不随源码提供；没有示例不代表系统配音或已配置云服务的文字生成不可用。

语音转字幕和画面字幕识别另需 whisper.cpp、RapidOCR/ONNX Runtime 等对应运行时及匹配模型。运行时准备以 `packaging/recognition-lock.json` 为准；模型可在软件中明确下载或离线导入，不随源码默认附送，也不会因为启动软件就自动下载。
