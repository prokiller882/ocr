# SnapOCR

Windows 离线截图 OCR 小工具：热键框选屏幕区域，即时识别文字，结果可编辑复制，历史可回看。全程本地运行，不联网、不上传任何数据。

## 为什么做这个

现有的截图 OCR 工具，遇到图片不是完全正面拍摄/扫描、文字带倾斜或透视变形时，经常识别不出来或准确率大幅下降。

SnapOCR 借助 [ppocrv6_onnx](https://github.com/AIwork4me/ppocrv6_onnx)（基于 [PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR)）的 PP-OCR 引擎，在「框选」这一步就解决文字歪斜的问题：

- **旋转选区**：选区可任意旋转，确认后自动反旋转摆正再识别，倾斜文字更准
- **异形框**：自由四边形框选 + 透视矫正，专门对付梯形 / 透视变形的文字区域

## 功能特性

- ⚡ 全局热键（默认 `Ctrl+Alt+Z`）或托盘双击，随时呼出截图
- 🖱️ 微信 / QQ 风格的框选遮罩（明亮选区 / 半透明灰两种风格，可在设置切换）
- 📐 选区可移动、缩放、旋转（Shift 吸附 15°），确认后自动摆正
- 🔷 异形框：4 个角点自由拖动贴合文字，透视矫正后识别
- 📝 结果窗口：源图 + 可编辑文本，一键复制，显示识别耗时
- 🕘 历史记录：自动保存 + 缩略图，翻页回看、单条删除 / 清空（二次确认）
- ⚙️ 设置：热键、自动复制、历史上限、OCR 参数（模型路径 / 阈值 / 预处理）
- 🔒 完全离线：识别模型随包分发，不调用任何云端 API

## 环境要求

- Windows（仅支持 Windows 平台）
- Python 3.10+

## 安装

```bash
# 1. 克隆仓库
git clone https://github.com/openuser-design/snap_ocr.git
cd snap_ocr

# 2. 安装依赖（requirements.txt 会安装本地 wheel 打包的 OCR 引擎）
pip install -r requirements.txt

# 3. 运行
python run.py
```

## 使用

1. 启动后程序常驻系统托盘
2. 按 `Ctrl+Alt+Z`（可在设置中修改），或双击托盘图标
3. 拖拽框选识别区域，松手后进入调整态，可移动 / 缩放 / 旋转微调
4. 遇到倾斜 / 透视变形的文字时，勾选操作条上的「异形框」，拖动 4 个角点贴合文字区域
5. `Enter` / 双击框内 / 点「确认」→ 自动识别，结果窗口显示可编辑文本
6. 点「复制全文」复制结果；托盘菜单可查看历史记录、修改设置

## 技术栈

- **语言**：Python 3.10+
- **界面**：Tkinter（标准库）
- **OCR**：ppocrv6_onnx（本地 wheel，自带 onnxruntime / OpenCV / NumPy / pyclipper）
- **托盘**：pystray
- **截图 / 图标**：Pillow
- **全局热键**：Windows RegisterHotKey（ctypes）
- **打包**：PyInstaller

## 目录结构

```
snap_ocr/
├── main.py            # 入口：装配 App + Tk 主循环
├── config.py          # 配置模型：读写 config.json
├── cores/             # 底座：路径解析 / 日志 / 全局热键
└── modules/           # 业务：托盘装配 / OCR 引擎 / 框选截图 / 历史 / 窗口
run.py                 # 开发态启动入口
requirements.txt       # 依赖锁定
```

## 致谢

- [ppocrv6_onnx](https://github.com/AIwork4me/ppocrv6_onnx) —— 本项目使用的 PP-OCR v6 ONNX 推理引擎
- [PaddlePaddle / PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR) —— 底层 OCR 算法与模型

## License

[MIT](LICENSE)
