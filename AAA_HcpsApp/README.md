# HcpsApp —— 多模态数据采集/分析系统（新壳）

从 `PyCharmMiscProject` 迁移出来的新工程，路线是**换壳保芯**。

- **换掉**：2000 行的巨型窗口类、全局 `threading.Event` 开关、采集线程直接写数据库、
  采集线程自己往 `127.0.0.1:8008` 转发给界面、硬编码盘符路径、一节多用的 `config.ini`。
- **保留**：设备协议解析（SmartEye 解包、心电帧解析、手套串口帧、gstreamer 管线）、
  写库字段结构、分析算法口径（HRV 特征、10 维负荷特征、三模型、眼动聚类）。

## 目录结构

```
HcpsApp/
├─ config/default.yaml            设备配置（一个设备一块）+ analyze 分析参数
├─ models/                        SVM / CatBoost / XGBoost / scaler + 表情识别 CNN3 权重
├─ core/
│  ├─ A0_data_collect/            采集层：设备 -> DataPacket
│  │  ├─ base.py                  基类：DataPacket / 生命周期 / 重连 / 统计
│  │  ├─ ecg.py  eye.py  emg.py   心电(TCP) / 眼动(UDP+SmartEye解包) / 肌电(LSL)
│  │  ├─ grip.py  video.py        握力(串口原始帧) / 视频(gstreamer 录像 + 逐帧)
│  │  ├─ camera.py                本机摄像头(cv2 直读，实时监测 + 可选落盘)
│  │  ├─ sim.py                   模拟数据源（无硬件开发与回归用）
│  │  └─ manager.py               采集总控：启停、分发、状态
│  ├─ A1_data_store/
│  │  └─ store.py                 SqliteStore(表结构与老库逐字一致) + FileStore(会话落盘)
│  ├─ A2_analyze/                 分析层：数据库/DataFrame -> 特征/结果（纯算法）
│  │  ├─ grip.py                  握力加工：原始帧 -> 老格式 data_grip_l/r
│  │  ├─ ecg.py                   心电 HRV 特征（小波去噪->滑窗->R峰->RR->8 特征）
│  │  ├─ eye.py                   眼动窗口特征（pupil / blink_rate）
│  │  ├─ load_model.py            负荷等级：10 维特征 -> SVM/CatBoost/XGBoost
│  │  ├─ pipeline.py              build_features / assess / analyze 一条龙
│  │  ├─ face.py                  表情识别：视频帧 -> 表情标签（CNN3 + BlazeFace）
│  │  └─ face_tools/              CNN3 网络结构 + BlazeFace 检测（搬自老 facial_tools）
│  ├─ A3_ui/                      界面层（新壳，PyQt5 + pyqtgraph，深色科技风）
│  │  ├─ theme.py                 配色 + 全局 QSS + 字体/素材
│  │  ├─ widgets.py               卡片 / 大数字块 / 导航项 / 图表面板 / 自绘压力条 / 信号桥
│  │  ├─ pages.py                 总览 / 采集 / 握力 / 分析 / 表情 / 设置
│  │  ├─ main_window.py           主窗口 + 采集会话控制
│  │  ├─ face_worker.py           表情识别子进程（tf 不放界面进程里）
│  │  └─ assets/                  校徽、实验室照片
│  └─ A5_link/
│     └─ ctrl.py                  采集卡主机 SSH 遥控（对时 / 起停 app_collect）
└─ tools/                         冒烟、握力、视频、分析、表情五条流水线 + 两个回归 + 合成会话
```

每层一个包，import 走包路径：

```python
from core.A0_data_collect import CollectorManager, SimCollector   # 包 __init__ 已聚合
from core.A0_data_collect.base import DataPacket                  # 也可直接用子模块
from core.A1_data_store import SqliteStore, FileStore
from core.A2_analyze import process_raw_table
```

## 命名规范

| 前缀 | 层 | 说明 |
|---|---|---|
| `A0_data_collect_*` | 采集层 | 每个连接方式一个文件，一个设备一个 Collector 类 |
| `A1_data_store_*` | 存储层 | 落库/落盘 |
| `A2_analyze_*` | 分析层 | 纯算法，DataFrame 进、结果出 |
| `A3_ui_*` | 界面层 | 新壳窗口 |
| `A4_alert_*` | 告警层 | 阈值判定 + 音频播报（待迁移） |
| `A5_link_*` | 对外链路 | 推理机双向报文、采集卡主机遥控（待迁移） |
| `A9_util_*` | 工具 | 日志、路径、配置等 |

## 老 -> 新 映射与状态

| 老文件 | 新文件 | 连接方式 | 状态 |
|---|---|---|---|
| `data_ecg.py` | `A0_data_collect_ecg.py` | TCP 服务端 | 已迁移 |
| `data_udp.py` | `A0_data_collect_eye.py` | UDP 服务端 + SmartEye 解包 | 已迁移 |
| `data_emg.py` | `A0_data_collect_emg.py` | LSL 流 | 已迁移 |
| `test.py` | `A0_data_collect_sim.py` | 模拟 | 已重写为模拟源 |
| `app.py` + `running_ctrl.py` | `A0_data_collect_manager.py` | — | 已重写 |
| `com/db.py` | `A1_data_store.py` | — | 已重写（表结构不变） |
| `data_grip.py` + `hand1.py` | `A0_data_collect_grip.py` + `A2_analyze_grip.py` | 串口 | 已迁移（方案二） |
| `data_video.py` | `A0_data_collect_video.py` | gstreamer 子进程 | 已迁移（帧同时落 data_video_frame） |
| `data_gpu.py` | `A0_data_collect_gpu.py` | TCP 双向 | 待迁移 |
| `alert.py` | `A0_data_collect_alert.py` | TCP + 音频 | 待迁移 |
| `collect_control_thread.py` | `A0_data_collect_ctrl.py` | SSH | 待迁移 |
| `load_analyze/ecg_process.py`（ECG 部分） | `A2_analyze_ecg.py` | — | 已迁移（逐值一致） |
| `load_analyze/ecg_process.py`（眼动部分） | `A2_analyze_eye.py` | — | 已迁移（逐值一致） |
| `load_analyze/load_assessment.py` | `A2_analyze_load_model.py` | — | 已迁移（三模型 + scaler） |
| `load_analyze/ecg_process.py::get_input` | `A2_analyze_pipeline.py` | — | 已迁移（含对齐规则） |
| `face_model`（CNN3 + BlazeFace） | `A2_analyze_face.py` | — | 已迁移（只搬正脸模型） |
| `eye_analyze/eye_analyze.py` | — | — | 待定：老代码读 Tobii 字段，新采集器是 SmartEye，字段对不上 |
| `script.py` / `form.py` / `*_window.py` | `A3_ui/`（pages.py） | — | 已重写（导航式，不再是 2000 行的 MyForm） |

已确认**可以丢弃**：`data_422.py` 与 `com/com_serial.py`（配置段 `[data_422_win]` 根本不存在）、
MQTT 相关配置（全项目无 paho 代码）、根目录重复的 `ecg_process.py` / `load_assessment.py`、
`window.py`、`save.py`、`performace_analyze/`（零引用）。

## 兼容契约（改之前务必看）

分析代码（`load_analyze/ecg_process.py`、`eye_analyze/eye_analyze.py`）是这样读库的：

```python
query = "SELECT * FROM data_ecg WHERE created_date BETWEEN ? AND ?"
ecg = json.loads(row[2])['ECG']    # row[2] 必须是 JSON 字符串
timestamp = row[3]                 # row[3] 是 created_date
```

因此新库必须满足：

1. 表名、列名、**列顺序**与老库逐字一致（`id, Mac_device, value, created_date` …）。
2. `data_ecg.value` = `json.dumps({"ECG": [...], "send_time": "..."})`（**JSON**，不是 repr）。
   老代码某些版本写进去的是 Python repr，分析端 `json.loads` 会直接报错——新工程按分析端要求写 JSON。
3. `data_udp.value` = SmartEye 解码字典 + `send_time` 的 JSON，以 **bytes** 存入（BLOB）。
4. `created_date` = `'%Y-%m-%d %H:%M:%S.%f'` 文本，SQLite 里按字符串比较时间也能正确排序。
5. 会话目录沿用老习惯：`实验结果/<用户名>_<任务>_<YYYYmmdd>/normal_result/database.db`。
6. 握力按**方案二**：`data_grip_raw` 存原始 18 通道；`data_grip_l/r` 由 `A2_analyze_grip`
   加工后写入（卡尔曼 → 角度解算 → 压力多项式拟合），字段格式与老库一致。

## 跑冒烟测试

```powershell
cd F:\hcpsproject\HcpsApp
python tools\smoke_test.py --seconds 5
```

无硬件也能跑：用模拟源产生心电/眼动数据，经由 `CollectorManager` 分发给 `SqliteStore`，
结束后打印每个采集器的状态、包数和落库行数。要接真心电设备时加 `--ecg-port 8001`。

分析层有自己的测试（同样不需要硬件，用 `tools/synth_session.py` 现造会话库）：

```powershell
$py = 'F:\hcpsproject\PyCharmMiscProject\.venv\Scripts\python.exe'
$dst = 'F:\hcpsproject\PyCharmMiscProject\AAA_HcpsApp'
& $py "$dst\tools\test_analyze_pipeline.py"          # 合成会话 -> 特征 -> 三模型负荷
& $py "$dst\tools\regression_analyze_vs_legacy.py"   # 与老 load_analyze 逐值对比
& $py "$dst\tools\test_face_pipeline.py"             # 表情识别（真脸图 + 会话逐帧）
```

分析接口就三个名字：

```python
from core.A2_analyze import build_features, assess, analyze

features = build_features(db_path, start, end, config['analyze'])   # 10 维特征
levels   = assess(features, config=config['analyze'])               # 三模型负荷等级
levels   = analyze(db_path, start, end, config=config['analyze'])   # 上面两步一条龙
```

## 跑界面

```powershell
$py = 'F:\hcpsproject\PyCharmMiscProject\.venv\Scripts\python.exe'
$dst = 'F:\hcpsproject\PyCharmMiscProject\AAA_HcpsApp'
& $py "$dst\tools\run_ui.py" --auto            # 模拟源，起来自动开始采集（无硬件）
& $py "$dst\tools\run_ui.py" --real            # 按 config/default.yaml 起真实采集器
& $py "$dst\tools\screenshot_ui.py"            # 离屏把六个页面截成 PNG，用于自检/存档
```

界面结构（`core/A3_ui/`）：左侧导航 + 顶部会话/时间条 + 页面栈 + 底部状态栏。

| 页面 | 内容 |
|---|---|
| 总览 | 四个状态块（心电/眼动/握力/已入库）+ 心电波形 + 瞳孔曲线 + 会话信息 |
| 采集 | 会话启停、采集器状态表、运行日志（logging 直接接进来） |
| 握力 | 左右手 15 路压力条 + 3 个关节角 + 合成力趋势（走 A2 的 GripProcessor） |
| 分析 | 选会话库 → 心电/眼动特征曲线 + 三模型负荷等级 + 特征表 |
| 表情 | 选图/示例图 → 检测框 + 八类概率（识别在子进程里跑） |
| 设置 | default.yaml 摘要、模型资产清单、打开目录 |

两条铁律（踩过坑）：**界面里不许出现设备协议和 SQL**；**别在非 GUI 线程碰控件**
（采集器线程的日志必须走 `LogBridge` 信号，否则偶发访问违例）。

### 摄像头实时监测

「表情」页底部就是实时监测面板：选摄像头编号 → 「打开摄像头」→ 边看边框脸边出表情。

* **会话在跑**：帧按 `analyze`/`camera` 配置落成 `camera1/cam1_%06d.jpg` 并进 `data_video_frame` 表 —— 监测即采集。
* **没开会话**：自动切成"只监测不落盘"，不污染数据库。
* 帧的 JPEG 字节直接放在 `packet.raw` 里，界面预览和表情识别都用它，不用回头读磁盘。
* 识别放在 `tools/face_cli.py` 子进程（TensorFlow 不进界面进程），实测约 130 ms/帧；
  `--serve` 常驻，第一帧要等模型热起来（5~10 秒）。
* 画面全黑会在面板上直接提示（帧亮度低于 `dark_threshold` 就标记），
  省得对着黑屏猜是隐私挡板、被占用还是没对准。

硬件不在时用假摄像头自检：`python tools\test_camera_pipeline.py`。

## 下一步

1. 补完 `A5_link`：ctrl 的 manager 接线 + 无硬件测试；`gpu`（与推理机的双向报文）待定义协议。
2. 迁移 `alert`（TCP 收 0/1 标志 + 播报警音）；它归 A4。
3. 眼动的注视/扫视口径（老 `eye_analyze` 是 Tobii 字段）需要定字段和阈值，再补 `A2_analyze_eye`。
4. 界面后续：实时曲线目前只画心电/瞳孔/握力，可以把表情、肌电、视频帧也接上；
   分析页想批量跑多个会话的话，把 `AnalyzePage._run` 拆到 QThread 里（现在同步跑，长会话会卡界面）。
