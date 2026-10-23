# 源码中的项目资产

这些文件是项目自己的图标和合成测试夹具，随本仓库按 MIT 提供。它们不包含用户素材、供应商音色示例、模型权重或第三方运行库。Qt、Chromium、FFmpeg 等软件本身仍适用各自许可证；本文件不替代其分发要求。

## 品牌图标

`packaging/resources/icons/yijianjifa.svg` 是项目白色几何剪刀、蓝紫色渐变的源图。四个派生文件来自该 SVG 和 `packaging/generate_icons.py`；本次公开源码复用项目已有生成结果，没有重新设计品牌。

从仓库根目录执行以下命令可重新渲染。开发环境需安装 PySide6 和 Pillow；生成 ICNS 还需要 macOS 系统的 `iconutil`。

```sh
python packaging/generate_icons.py
```

| 文件 | SHA256 |
| --- | --- |
| `packaging/resources/icons/yijianjifa.svg` | `eca98971823e8088192281ad9b59680984f8663eae29654b047e248b579b63f7` |
| `packaging/resources/icons/yijianjifa.png` | `8342734e3c946585ab0ca0460147bbc31d9afb5b8d3d3503f809147fb387feea` |
| `packaging/resources/icons/yijianjifa.ico` | `1d74ab946449932abf4d9f8fbaaf3087c6bc085ed7f3bdd6cc592fe9107ff3a6` |
| `packaging/resources/icons/yijianjifa.icns` | `bcd6925ca11e35573dfff066405c3d0f6d5f0e60c0f6cbee4be1196af458b715` |
| `src/yijianjifa/webui/assets/app-icon.png` | `2e23a0699c6dd60946d133f19a13434e4097347a9a7c24b8f43cfbb283ff811f` |

图形库、编码器版本可能影响派生字节，表中记录的是本仓库实际提交的文件。

## 能力检查测试片

`src/yijianjifa/webui/assets/capability-test.mp4` 是此次公开源码新建的蓝色纯色片：3 秒、720×1280、25fps、H.264、无声音轨、5,592 字节。不读取相机、用户视频、网络或供应商素材。生成命令如下（从仓库根目录执行；`-n` 不覆盖已有文件）：

```sh
ffmpeg -hide_banner -loglevel error -nostdin -n \
  -f lavfi -i color=c=0x435ff5:s=720x1280:r=25:d=3 \
  -an -c:v libx264 -preset medium -crf 23 -pix_fmt yuv420p \
  -map_metadata -1 -movflags +faststart -fflags +bitexact -flags:v +bitexact \
  src/yijianjifa/webui/assets/capability-test.mp4
```

此次生成使用 FFmpeg 8.1.1。文件 SHA256 为 `d87ff718eed1660d0df005eeb78626848ba29b956a66d1d38ac4055a9cc9ba2a`；`features/accounts/capability_probe.py` 保持固定文件校验，缺失或字节变化一律拒绝。不同编码器版本生成不同字节时，应审查新片、同步该常量并复测，不应移除校验。

可用 `ffprobe -v error -show_streams -show_format -of json src/yijianjifa/webui/assets/capability-test.mp4` 核对元信息。能力检查仍需用户明确允许上传此固定片，片子本身不会自动触发登录、上传或发布。

## 独立界面诊断片

`tools/webui_probe/assets/probe.mp4` 是项目原有独立桌面壳实验的合成媒体，3 秒、320×180、15fps、H.264/AAC、92,026 字节。它用于本地解码、播放和定位诊断，不是用户业务视频，也不是云配音样例。

原有诊断记录明确对应上述时长、分辨率和约 92KB 大小。本次原字节纳入源码，SHA256 为 `71fa45041bf1e1d5af1a3901f27a6b96f5d10cd374122b414bfdccd1f12fbd3d`。没有保留其最初的逐参数生成命令，因此不宣称能由命令重建完全相同字节；本地诊断可以直接读取仓库内这份固定文件。

本仓库不包含云供应商的官方 MP3 示例。需要音色样例或第三方依赖时，应另外取得对应权限或按项目配置说明使用自己的服务。

## 临时合成测试音频

`tests/conftest.py` 的 `synthetic_mp3` 夹具在测试临时目录内，使用 FFmpeg 的 `lavfi sine` 生成 440Hz、24kHz、1 秒的测试音，并编码为 MP3。部分音色路径测试采用同类临时生成方式。测试只核验本地媒体读取、缓存、恢复和模拟服务行为，不包含人声、供应商示例或收费调用；生成文件不提交进 Git。

## 快手声明事实夹具

`tests/fixtures/kuaishou-author-declarations-20261009.json` 保存从快手网页前端资料提取的四组声明名称及枚举值，不包含完整第三方脚本。夹具记录公开来源 URL、采集日期、构建标识、原始资料 SHA256 和偏移位置；测试还固定校验该 JSON 的 SHA256，再将独立事实与实现逐项比对。它仅用于离线映射验证，不证明平台后续版本未变化。
