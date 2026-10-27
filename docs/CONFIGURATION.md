# 源码与官方发行配置

## 本地免费使用

源码默认提供现有云配音服务入口 `https://39.108.82.213`，用户不必自行录入供应商密钥即可注册、登录和充值使用。系统配音、导入、混剪、字幕、预览、导出与平台发布仍不依赖云配音账号；默认系统配音不会自动改用收费云音色。发布需登录自己的平台账号。

`--data-dir`可指定隔离数据目录。不要将真实数据库、浏览器profile、令牌、用户素材或正式经营配置放入源码树；也不要通过公开Issue或报告分享这些文件。

## 可选云配音

使用现有云配音业务不需要配置环境变量。用户主动登录并选择云配音后，按服务字数规则使用；打开软件或使用系统配音不会自动发起收费合成。构建者也可通过环境变量`P011_CLOUD_VOICE_ENDPOINT`显式设置自己的HTTPS服务入口。空字符串明确关闭云配音。

示例（仅域名示意，不能直接作为可用服务）：

```sh
export P011_CLOUD_VOICE_ENDPOINT=https://voice.example.test
python -m yijianjifa.webui.mac_app --data-dir /path/to/isolated-data
```

环境变量只接受不含用户名、密码、query或fragment的HTTPS地址。无效可选配置保持云服务关闭，不妨碍本地免费功能。测试服务的loopback例外通过现有专用测试入口使用，不能将不安全远程HTTP当正式配置。

配音账号令牌仍保存在本机安全凭据服务中。账本、兑换码与付费字数由所配置服务管理，源码不附带供应商密钥或免费云额度。配置到同一入口并使用同一数据目录时，原账号凭据命名空间不变。

## 官方冻结发行包

经营者可在构建时显式设置`P011_RELEASE_CLOUD_VOICE_ENDPOINT`。`packaging/release_configuration.py`只将非秘密HTTPS入口写入本次生成的`cloud-voice-service.json`，由Windows/Mac spec收集。未设置时不会拾取上次生成的旧配置。

运行时的`P011_CLOUD_VOICE_ENDPOINT`优先于包内配置，包内配置优先于源码内置的现有服务入口；显式空值可关闭服务。仅冻结运行会读取包内配置；源码运行不会隐式读取旧构建目录中的配置。无效的显式配置保持关闭，不会悄悄改连其他服务；没有配置时沿用内置入口。

官方构建仍须显式配置并验收原服务入口，确保已有账号与余额使用原命名空间。源码或第三方构建未提供覆盖配置时，也可直接对接现有业务；没有登录并主动使用云配音，不会自动发起收费合成。

Windows 官方工作流在仓库标识为`tianyiswl/yijianjifa-desktop`时写入原服务入口，也可用非秘密仓库变量`P011_RELEASE_CLOUD_VOICE_ENDPOINT`覆盖。其他仓库未设置变量时，不生成覆盖配置，程序仍沿用内置业务入口；设置变量后可改连自有服务。该接线没有变更任何线上仓库变量或服务。

Windows 官方构建使用`packaging/windows/build.ps1 -Official`；Mac 本地官方构建使用`packaging/macos/build.sh --official`，并要求先在环境中显式设置`P011_RELEASE_CLOUD_VOICE_ENDPOINT`。通用构建入口自身不含经营者服务默认值。官方模式缺少入口时会在构建前失败，防止意外生成关闭老用户云配音的官方包。

两个入口均调用`packaging/check_release_configuration.py`：先离线检查构建配置，冻结后再核对实际包内`cloud-voice-service.json`与本次入口完全一致。通用未配置构建还会拒绝误混入的旧服务配置。可单独检查配置而不执行原生构建：

```sh
python packaging/check_release_configuration.py --official
bash packaging/macos/build.sh --official --check-configuration
```

Windows PowerShell 对应命令为`./packaging/windows/build.ps1 -Official -CheckConfiguration`。这些检查不请求服务、不登录、不扣费；通过只说明配置接线和文件对应，不能替代Windows/Mac实际构建、旧账号升级与余额保留验收。

私有供应商密钥、管理密码哈希、兑换码密钥和正式数据库仅留在服务器外置配置中。云端代码可按源码公开策略审阅，运行配置不包含在MIT源码或免费包里。

## 公开发布的剩余门槛

源码内MIT仅适用于有权授权的项目代码；Qt、FFmpeg、浏览器和媒体等保留原许可。新下载入口、源码仓库地址、可公开媒体与完整第三方材料在经营者确认后落地。

本地测试与暂存包不等于已公开、已发布或已完成Windows冻结验收。正式公开前必须人工审阅候选文件、许可证、资产权属、测试报告、构建版本及SHA256。
