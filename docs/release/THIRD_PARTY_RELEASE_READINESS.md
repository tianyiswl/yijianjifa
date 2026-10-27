# 第三方许可与程序包发行准备

本仓库的自研源码采用根目录 [MIT 许可证](../../LICENSE) 公开。软件及系统配音免费，云配音仍为可选收费服务。**源码公开与包含第三方二进制的程序包发行是两件事。** 本次仓库更新仅包含源码，不附带 Windows/Mac 可执行程序或第三方运行库。

## 源码仓库的许可范围

项目 MIT 适用于有权授权的自研代码。第三方源码、许可/NOTICE 文本、系统声音、商标、模型和外部媒体保留各自条款，不改授 MIT。

仓库保留 `packaging/resources/licenses/` 中的上游许可文本及来源/哈希清单，用于审阅与后续构建核对。它们不是当前所有目标系统的完整 SBOM，也不证明某个二进制包已满足分发义务。依赖由使用者按项目配置另行安装；仓库不附 PySide/Qt、Chromium、FFmpeg、Node、VC++ 运行库或模型权重二进制。

官方音色示例 MP3 未随源码分发。项目品牌 SVG、生成图标及专门合成的测试媒体不包含真实用户或供应商样音。调用系统配音不代表取得系统声音文件的再分发许可。

## 程序包发行前仍需完成

| 范围 | 需要核对的材料 |
| --- | --- |
| PySide / Qt / Qt WebEngine | 实际目标版本的许可、第三方声明与所选条款要求的对应源码及替换/重链接安排。Qt WebEngine 与独立浏览器分别核验。 |
| Playwright / Chromium / Node | 实际版本、完整 LICENSE/NOTICE/credits/terms；Playwright 的 Apache-2.0 不统一覆盖浏览器及其原生依赖。 |
| 产品 FFmpeg / ffprobe | 准确构建、启用组件、对应源码、补丁和构建信息；不得把带 GPL 组件的版本称作 LGPL-only。 |
| 浏览器辅助 FFmpeg | 独立核对其版本和材料，不能用产品 FFmpeg 的材料代替。 |
| Windows VC++ / OpenMP | 每个实际 DLL 的版本、来源和适用再分发条件。 |
| whisper.cpp / RapidOCR / ONNX Runtime / 模型 | 运行时和模型各自的版本、许可、来源与哈希；下载清单引用不意味着已随软件授予或分发。 |
| 字体、样音、图标及演示媒体 | 明确来源和可再分发范围；未确认内容继续排除或换成有权分发的资产。 |

实际产物需带完整许可文本与准确文件清单，完成对应系统构建、设备测试并记录源码提交及 SHA-256。不能用旧版本或另一操作系统的材料替代。仓库中已有清单的 `release_ready=false` 保留为二进制分发尚未闭环的说明，不否认此次 MIT 源码公开。

官方查阅入口：[Qt for Python](https://doc.qt.io/qtforpython-6/licenses.html)、[Qt 义务说明](https://www.qt.io/development/open-source-lgpl-obligations)、[Qt WebEngine](https://doc.qt.io/qt-6/qtwebengine-licensing.html)、[FFmpeg](https://ffmpeg.org/legal.html)、[VC++ 再分发](https://learn.microsoft.com/en-us/cpp/windows/redistributing-visual-cpp-files)。这些入口是核查指引，不能代替目标版本的材料或全面合规结论。

## 许可文本完整性检查

```sh
python tools/verify_release_licenses.py
```

该脚本只核对已有清单引用文本及哈希，不下载组件、不修改许可或把 `release_ready` 改为 true。通过仅表示这些文本完整，不表示所有未列入的组件和分发要求已完成。
