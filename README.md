# 一剪即发

自动剪辑、自动发布，多平台、多账号统一安排。

导入自己拍摄的视频和准备好的文案，设置配音、字幕、音乐与发布时间，制作视频并向所选平台提交发布安排。适合需要持续制作和发布产品视频的商家、工厂及个人创作者。

## 开源与免费

[本仓库](https://github.com/tianyiswl/yijianjifa)公开 **0.1.42** 源码，项目自研代码采用 [MIT 许可证](LICENSE)。

- **软件和系统配音免费**，不限制使用年限，不需要软件激活码或云配音账号即可使用本地功能。
- **云配音可选收费**，只有主动配置并使用云服务时才需要配音账号和字数。生成文字试听也可能消耗字数。
- 本次发布源码，**暂未提供新版免费 Windows/Mac 程序包**。GitHub 的 “Download ZIP” 下载的是源代码，不能解压后直接运行 exe。

第三方依赖、系统音色和外部媒体各自适用原有条款，不能因本仓库使用 MIT 就将它们全部视为 MIT。见[第三方说明](docs/release/THIRD_PARTY_RELEASE_READINESS.md)。

## 主要功能

- 素材与文案管理、自动混剪、配音、字幕、背景音乐和画面设置。
- 抖音、快手、视频号和 B 站视频发布，同一平台可管理多个账号。
- 设置日期、天数、每天条数和时间间隔，制作后提交平台发布安排。
- 成片预览、导出与任务记录；需要登录或验证时按提示处理。
- 视频下载与本地视频提取工具。字幕识别另需相应运行时和模型。

使用自己的素材、文案及有权使用的平台账号。平台验证、审核和发布限制仍然适用；提交、平台接收与最终公开展示是不同状态。本工具不保证流量或收益。

## 从源码启动

需要 Python 3.12、可用的 **FFmpeg 与 ffprobe**；发布功能还需要 Playwright Chromium。Windows 使用 Qt WebEngine 界面，macOS 使用 Cocoa/WebKit 界面。

完整的环境安装、隔离启动和操作步骤见[快速开始](docs/QUICK_START.md)。云配音默认关闭，显式启用和构建配置见[配置说明](docs/CONFIGURATION.md)。

官方音色示例 MP3 不随源码分发；界面会按本机资源情况显示是否有示例。系统配音正常使用不需要这些文件。云音色文字试听需先配置服务，按服务计费规则执行。

识别运行时、模型、第三方浏览器和媒体工具不作为仓库内容附送。需要字幕识别时，按 `packaging/recognition-lock.json` 准备运行时，并在软件中明确下载或导入匹配模型。

## 开发验证

安装开发依赖后，可运行免费模式相关测试与界面测试：

```sh
python -m pytest tests/unit/mixing/test_free_cloud_configuration.py tests/integration/webui/test_use_term.py -q
node --test tests/webui/*.test.cjs
```

完整 Python 测试入口为 `python -m pytest`；原生界面、媒体和识别检查需要对应操作系统及可选依赖。请区分单项测试、完整源码回归和 Windows/Mac 实机验收。源码检查通过不能证明真实平台已经发布成功。

本次测试结果、跳过项和范围见[源码验证记录](docs/VALIDATION.md)，仓库内资源来源见[源码资源说明](docs/SOURCE_ASSETS.md)。

云服务隔离测试见 [server/cloud_voice](server/cloud_voice/README.md)。真实账号、验证码、数据库、密钥和用户素材不要提交至 Git 或公开 Issue。

## 构建与发行

仓库保留 Windows/Mac 构建脚本。构建需要自行准备锁定的媒体、浏览器、识别运行时等输入，并生成图标；详见[配置说明](docs/CONFIGURATION.md)和 [Windows 验收说明](packaging/windows/测试说明.md)。

**源码已公开不代表新免费程序包已经发行。** 二进制发行仍需完成目标系统测试、第三方分发材料、准确依赖清单与文件哈希核验。不得用旧版本包或单个平台的测试结果替代新版验收。
