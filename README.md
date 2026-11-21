# PyCharmMiscProject

本目录现在只保留**在用的工程**，历史代码、数据和构建产物统一收到 `_archive/`。

```
PyCharmMiscProject/
├── AAAwindows_code/        ★ 当前主线：BLE 心电/手环 + 摄像头采集、实时界面、特征分析
│   └── tools/                配套工具：ble（心电诊断）、eye（眼动）、camera、db、vision
├── face_tool/              ★ 面部 ROI 离线处理工具（独立 GUI/CLI，不依赖主程序）
├── AAA_HcpsApp/             另一套分层重构（A0 采集 / A1 存储 / A2 分析 / A3 界面）
├── .venv/                   两个工程公用的解释器（不要移动，preflight 与 PyCharm 都指向它）
├── _archive/                历史材料（不进版本库）
│   ├── legacy_app/          老主程序：app.py / window.py / script.py + 全部运行时资源
│   ├── data/                历史会话库、实验结果、任务原始落盘、错误日志
│   └── build_output/        PyInstaller 产物（dist / dist_1 / dist_sd / build）
├── 版本管理.md               版本号规则、提交与回退流程
├── 代码审查报告.md           对整个历史仓库的通读与缺陷清单
└── .gitignore
```

## 怎么跑

**主线（推荐）**

```powershell
cd AAAwindows_code
..\.venv\Scripts\python.exe run_monitor.py      # 带界面
..\.venv\Scripts\python.exe main.py             # 纯采集
```

**老主程序**（仍在 `_archive/legacy_app/`，需要真实硬件）

```powershell
cd _archive\legacy_app
F:\hcpsproject\PyCharmMiscProject\.venv\Scripts\python.exe app.py
```

> 老代码用**相对当前工作目录**的路径找 `config.ini`、`saved_models/`、`异常状态预警音/`、
> `gstreamer/1.0/...`，所以**必须先 `cd` 进 `legacy_app` 再启动**，不能在别的目录里执行它的绝对路径。

## 配套工具

这些工具都独立运行，不改动主程序，也不进主程序的打包产物。

| 工具 | 入口 | 用途 |
|---|---|---|
| 心电贴分级诊断 | `python AAAwindows_code\tools\ble\diag_ecg.py --name E122...` | 扫描→建链→服务发现→特征→握手→收包，逐段定位卡点 |
| 心电贴检测界面 | `python AAAwindows_code\tools\ble\ecg_probe_gui.py --auto E122...` | 实时波形、丢包率、体温、MTU、退避重连 |
| 眼动工具箱 | `python AAAwindows_code\tools\eye\eye_toolkit_gui.py` | 实时入库 / 录制 CSV / 导入到库 / 回放测试 |
| 眼动独立采集 | `python AAAwindows_code\receive_tcp_5001.py` | 连接 192.168.2.98:7901，原样落盘 |
| 面部 ROI 处理 | `python face_tool\run_gui.py` 或 `run_cli.py` | 会话目录 → 眼/嘴 ROI + 68 点 + 逐帧指标 |

## 版本控制

- 规则见 `版本管理.md`，当前基线 **v1.0.0**（对应 2026-11-15 打包版本）
- 只跟踪源码与文档；`data/`、`dist/`、模型权重、采集原始数据一律不入库
- 打版本：`git tag -a v1.1 -m "说明"`；回退：`git switch --detach v1.0.0`

## 归档说明

- `_archive/` 已在 `.gitignore` 里整段忽略，约 10 GB，不参与版本控制。
- 若以后要重新纳入版本控制：删掉 `.gitignore` 的 `_archive/` 一行，并在 `_archive/legacy_app/`
  下加一个 `.gitignore` 排除 `gstreamer/`、`face_model/`、`saved_models/` 三个大目录。
- 归档用的分类脚本在 `_archive/_reorganize.py`，改分类规则后可以重跑（默认 dry-run）。
