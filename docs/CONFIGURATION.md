# 源码、云服务与构建配置

## 本地数据和依赖

源码默认使用免费的本地功能和系统配音，不连接任何经营者的云配音服务。平台发布需要登录自己的平台账号。

使用 `--data-dir` 指定独立的绝对数据路径，启动示例见[快速开始](QUICK_START.md)。会话、数据库、音频缓存和用户素材不要放进源码树；不要通过公开 Issue 分享这些文件。

源码运行需安装 Python 依赖及 FFmpeg、ffprobe。发布依赖 `.[publish]` 和 `python -m playwright install chromium`；macOS 界面另需 `.[mac-webui]`。识别功能另需 `.[recognition]`、锁定的 native 运行时和清单匹配的模型，不因安装基础桌面依赖就准备完毕。

## 可选云配音

`P011_CLOUD_VOICE_ENDPOINT` 显式指定服务 HTTPS 入口；未设置或设置为空时关闭云配音。示例域名仅为说明，不能直接作为可用服务：

```sh
export P011_CLOUD_VOICE_ENDPOINT=https://voice.example.test
.venv/bin/python -m yijianjifa.webui.mac_app --data-dir "$HOME/Library/Application Support/YiJianJiFa-Source-Test"
```

Windows PowerShell 可用 `$env:P011_CLOUD_VOICE_ENDPOINT = 'https://voice.example.test'` 后执行相应 Windows 启动命令。

入口不能包含用户名、密码、query、fragment、控制字符或无效端口。无效可选配置会保持服务关闭，不影响本地免费功能。专用本机测试入口可使用明确的 loopback 测试配置，不能据此给正式远程服务配置明文 HTTP。

账号令牌保存在本机安全凭据服务中。字数与账本由配置的服务管理，源码不附供应商密钥或免费云额度。使用相同入口与相同数据目录时，凭据命名空间保持一致；更换入口不会迁移云端账号、余额或原任务。

自行部署云服务见 [server/cloud_voice](../server/cloud_voice/README.md)。供应商权限、可用音色、价格和服务保障由部署者另行核实，不能把源码公开当成提供了可直接使用的云服务。

## 冻结程序的服务配置

构建时可显式设置 `P011_RELEASE_CLOUD_VOICE_ENDPOINT`。`packaging/release_configuration.py` 将这个非秘密入口写入本次生成的 `cloud-voice-service.json`，由 Windows/Mac spec 收集。未配置时不会拾取上次构建留下的旧配置。

冻结程序优先读取运行时的 `P011_CLOUD_VOICE_ENDPOINT`，显式空值可关闭服务；否则读取包内默认值。源码运行不读取冻结包的默认服务文件。

提供自有正式云服务的构建可使用：

- Windows：`./packaging/windows/build.ps1 -Official`
- macOS：`bash packaging/macos/build.sh --official`

该模式要求先显式设置 `P011_RELEASE_CLOUD_VOICE_ENDPOINT`。通用构建脚本本身不包含经营者默认地址。构建前检查配置，构建后核对实际包内入口；检查不请求服务、不登录或扣费。

只检查配置而不构建：

```sh
python packaging/check_release_configuration.py
```

正式模式检查加 `--official`。macOS 构建脚本可用 `--check-configuration`；Windows 对应 `-CheckConfiguration`。macOS 脚本默认解释器路径面向预先准备的构建环境，可用 `P011_BUILD_PYTHON` 明确指定当前构建解释器。

## 构建输入与发行范围

构建前运行 `python packaging/generate_icons.py`，由仓库中的品牌 SVG 生成 PNG/ICO；macOS 同时生成 ICNS。该脚本需要 PySide6 和 Pillow，macOS 的 ICNS 导出调用系统 `iconutil`。

FFmpeg/ffprobe、Playwright 浏览器、whisper.cpp、Windows VC++/OpenMP 运行库等不随仓库附送。按构建脚本和 `packaging/recognition-lock.json` 准备对应输入，不能把已有文件或旧包检查通过当成新版可分发的证明。官方音色示例未随源码提供，不应从旧私有包中自动搬入公开发行包。

项目 MIT 只覆盖有权授权的自研代码。发布包含第三方二进制的程序包前，仍需准备准确版本、许可和对应源码/构建材料，完成目标系统与真实平台验收。见[第三方交付审查](release/THIRD_PARTY_RELEASE_READINESS.md)和 [Windows 验收说明](../packaging/windows/测试说明.md)。

当前源码公开不包含新版免费程序包发布，也不承诺保留的构建脚本已在所有环境一键完成构建。构建产物应记录源码提交号、平台、依赖版本及 SHA-256。
