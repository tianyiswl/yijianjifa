# 一剪即发品牌图标

`yijianjifa.svg` 是源文件，沿用客户端白色剪刀及蓝紫色品牌。外部透明，1024 方图预留边缘。

- PNG：1024px 品牌预览。
- ICO：Windows 可执行文件及安装包，包含 16/24/32/48/64/128/256px。
- ICNS：Mac 应用包，包含 16/32/128/256/512px 和对应双倍分辨率。
- `src/yijianjifa/webui/assets/app-icon.png`：256px Windows 窗口与任务栏资源，由现有 assets 打包规则收集。

修改 SVG 后，用已配备 PySide6、Pillow 的开发 Python 运行 `packaging/generate_icons.py`。Mac 上使用系统 iconutil 生成 ICNS；Windows 上保留已提交的 ICNS。

Mac spec 的 BUNDLE、Windows spec 的 EXE、Inno Setup 的 SetupIconFile 均指定专用文件；运行中的 Windows QApplication 也设置窗口图标。旧包不会自动获得新图标，需要重新打包。
