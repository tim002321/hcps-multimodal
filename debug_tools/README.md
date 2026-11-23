# debug_tools — 内部排障与自检工具

这里放的是**我们自己用来排查问题、验证数据、标定阈值**的工具，**不随客户版发布**。

与主程序的关系只有一条：读它的输出（`config.ini`、`models/`、会话库、日志），**不 import 主程序模块，也不被主程序 import**。
这样任一方的改动都不会把另一方带崩。

## 目录

| 目录 | 内容 | 典型用途 |
|---|---|---|
| `ble/` | `diag_ecg.py` 六阶段分级诊断、`ecg_probe_gui.py` 实时检测界面、`ble_scan.py`、`ble_retry_test.py` | 心电贴连不上、掉线、波形可疑 |
| `eye/` | `receive_tcp_5001.py` 采集、`eye_live_gui.py` 实时入库、`import_eye_csv.py` 导入、`replay_eye_csv.py` 回放、`eye_toolkit_gui.py` 四页签工具箱 | 眼动仪数据落库与验证 |
| `db/` | `db_overview.py` 库概览、`merge_camera_db.py` 合并、`replay_check.py` 回放检查、`cleanup_empty.py` 清理空库、`test_*.py` | 会话库结构 / 数据完整性 |
| `camera/` | `camera_scan.py` 设备枚举、`export_camera_frames.py` 导帧 | 摄像头不出图、帧缺失 |
| `vision/` | `fetch_models.py` 下权重、`calibrate_threshold.py` 标定阈值、`bench_yawn.py` 离线基准、`live_probe.py` 实时概率 | 视觉模型接入与调参 |
| 根目录 | `test_yawn_pipeline.py`、`test_eye_protocol.py`、`ui_screenshot.py` | 纯逻辑断言、界面截图冒烟 |

## 路径约定

所有脚本都在运行时按 `__file__` 推导主程序目录，规则是**上溯到仓库根再拼 `AAAwindows_code`**：

```
debug_tools/ble/diag_ecg.py      → ../../AAAwindows_code
debug_tools/test_yawn_pipeline.py → ../AAAwindows_code
```

因此：

- 整个仓库搬到任何位置都能用，**不需要改路径**
- 不要把 `debug_tools/` 复制进 `AAAwindows_code/`，否则推导会多一层
- 新增脚本时照抄同目录已有脚本的 `ROOT` 写法，不要硬编码绝对路径

## 常用命令

```bash
# 心电贴：先确认在广播，再分级诊断卡在哪一步
.venv\Scripts\python.exe debug_tools\ble\diag_ecg.py --name E1220511000212 --listen 8
.venv\Scripts\python.exe debug_tools\ble\ecg_probe_gui.py --auto E1220511000212
.venv\Scripts\python.exe debug_tools\ble\ecg_probe_gui.py --demo        # 无硬件看界面

# 眼动：采集 → 导入 → 回放
.venv\Scripts\python.exe debug_tools\eye\eye_toolkit_gui.py             # 四页签工具箱
.venv\Scripts\python.exe debug_tools\eye\receive_tcp_5001.py --selftest # 报文解析自检

# 会话库
.venv\Scripts\python.exe debug_tools\db\db_overview.py

# 视觉模型
.venv\Scripts\python.exe debug_tools\vision\fetch_models.py --check

# 纯逻辑断言（不需要硬件）
.venv\Scripts\python.exe debug_tools\test_yawn_pipeline.py
.venv\Scripts\python.exe debug_tools\test_eye_protocol.py
```

## 新增工具的约定

1. **必须独立可跑**：`python <脚本> --help` 能出帮助，不依赖主程序已启动
2. **默认只读**：读库、读配置、读模型；需要写库的工具（如 `import_eye_csv.py`）必须默认 dry-run、显式开关才写
3. **不 import 主程序 UI**：需要主程序的纯函数时，把 `AAAwindows_code` 插进 `sys.path` 再 import 具体模块
4. **不进包**：打包脚本显式排除本目录；`eye_build/` 只打包眼动工具箱那一个入口

## 与客户版的边界

客户版 `HcpsMonitor.exe` 内**没有**这里的任何脚本，也没有入口。现场排障只有两条路：

- 看 `meta.json` 与会话库统计（主程序保留的元数据）
- 把会话目录拷回来，用本目录的工具离线分析
