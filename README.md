# PyCharmMiscProject

本目录现在只保留**在用的工程**，历史代码、数据和构建产物统一收到 `_archive/`。

```
PyCharmMiscProject/
├── AAAwindows_code/        ★ 当前主线：BLE 心电/手环 + 摄像头采集、实时界面、特征分析
├── AAA_HcpsApp/             另一套分层重构（A0 采集 / A1 存储 / A2 分析 / A3 界面）
├── .venv/                   两个工程公用的解释器（不要移动，preflight 与 PyCharm 都指向它）
├── _archive/                历史材料（不进版本库）
│   ├── legacy_app/          老主程序：app.py / window.py / script.py + 全部运行时资源
│   ├── data/                历史会话库、实验结果、任务原始落盘、错误日志
│   └── build_output/        PyInstaller 产物（dist / dist_1 / dist_sd / build）
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

## 归档说明

- `_archive/` 已在 `.gitignore` 里整段忽略，约 10 GB，不参与版本控制。
- 若以后要重新纳入版本控制：删掉 `.gitignore` 的 `_archive/` 一行，并在 `_archive/legacy_app/`
  下加一个 `.gitignore` 排除 `gstreamer/`、`face_model/`、`saved_models/` 三个大目录。
- 归档用的分类脚本在 `_archive/_reorganize.py`，改分类规则后可以重跑（默认 dry-run）。
