# HcpsApp 交接摘要

> 给"新会话/新接手的人"看的：读完这份 + `README.md`，就能接着干。
> 最后更新：2026-09-15

## 0. 先解决环境问题（最重要）

当前旧会话的项目根目录绑在 **已不存在** 的 `F:\新建文件夹\PyCharmMiscProject` 上，
导致写文件每次都要授权、`view_image` 直接报 `os error 267`。

**新会话请把项目目录打开成真实路径：**

- `F:\hcpsproject\PyCharmMiscProject`（老工程，里面能看到新工程文件夹）
- 或直接 `F:\hcpsproject\PyCharmMiscProject\AAA_HcpsApp`

## 1. 这个工程是什么

从老工程 `PyCharmMiscProject` 里**换壳保芯**出来的新工程，位置在
`F:\hcpsproject\PyCharmMiscProject\AAA_HcpsApp`。

- **保留**：设备协议解析（SmartEye 解包、心电帧、手套串口帧、gstreamer 管线）、
  写库字段结构、分析算法口径。
- **换掉**：2000 行的巨型窗口类、全局 `threading.Event`、采集线程直接写库、
  采集线程自己发 UDP 8008 给界面、硬编码路径、一节多用的 `config.ini`。

## 2. 已完成（7 个测试全绿）

```
AAA_HcpsApp/
├─ config/default.yaml            一个设备一块的配置 + analyze 分析参数
├─ models/                        老工程 saved_models 拷来的模型（SVM/CatBoost/XGBoost/scaler + 表情 CNN3）
├─ core/
│  ├─ A0_data_collect/            base / ecg / eye / emg / grip / video / camera / sim / manager
│  ├─ A1_data_store/store.py      SqliteStore + FileStore + session_dirs
│  ├─ A2_analyze/                 grip / ecg / eye / load_model / pipeline / face + face_tools
│  ├─ A3_ui/                      主窗口 + 六个页面 + 主题 + 控件（深色科技风）
│  └─ A5_link/ctrl.py             采集卡主机 SSH 遥控（迁移中，见第 4 节）
└─ tools/                         smoke / grip / video / analyze / face + 两回归 + 合成会话 + 界面
```

- **A0 采集 5/5 完成**：心电(TCP 服务端)、眼动(UDP+SmartEye 解包)、肌电(LSL)、
  握力(串口原始帧)、视频(gstreamer 子进程 + 逐帧发包)，外加模拟数据源和总控。
- **A1 存储完成**：表结构与老库逐字一致；新增两张表 `data_grip_raw`、`data_video_frame`；
  会话目录规则集中在 `session_dirs()`；文件落盘交给 `FileStore`。
- **A2 握力加工完成**：`A2_analyze/grip.py`，与老 `hand1.process_frame` 逐帧比对偏差为 0。
- **A2 分析层完成**（2026-09-15）：
  - `ecg.py` 心电 HRV 特征、`eye.py` 眼动窗口特征、`load_model.py` 三模型负荷等级、
    `pipeline.py` 串成 `build_features / assess / analyze`；与老 `load_analyze` **逐值一致**（回归测试）。
  - `face.py` 表情识别（CNN3 + BlazeFace，只搬了正脸模型），逐帧出表情表。
  - 模型文件在 `models/`（含 86MB 的表情权重）；分析参数统一在 `config/default.yaml` 的 `analyze` 段。
- **A5_link ctrl 半成品**：`core/A5_link/ctrl.py` 已落盘（能 import），但 manager 接线和测试还没做。
- **A3 界面完成**（2026-09-15）：`core/A3_ui/`（总览 / 采集 / 握力 / 分析 / 表情 / 设置），
  深色科技风、左侧导航 + 顶部信息条；素材用上了老工程的校徽与实验室照片（`A3_ui/assets/`）。
  采集直接订阅 A0 的 sink，分析直接调 A2，界面里没有协议与 SQL。

验证命令（用老工程的 venv 跑）：

```powershell
$py = 'F:\hcpsproject\PyCharmMiscProject\.venv\Scripts\python.exe'
$dst = 'F:\hcpsproject\PyCharmMiscProject\AAA_HcpsApp'
& $py "$dst\tools\smoke_test.py" --seconds 3            # 心电+眼动：应入库几十条
& $py "$dst\tools\test_grip_pipeline.py" --seconds 3   # 握力：原始帧 -> 老格式表
& $py "$dst\tools\test_video_pipeline.py" --seconds 3  # 视频帧：发包数 == 落库行数
& $py "$dst\tools\regression_grip_vs_legacy.py"        # 握力算法与老实现偏差应为 0
& $py "$dst\tools\test_analyze_pipeline.py"            # 分析层：合成会话 -> 特征 -> 三模型负荷
& $py "$dst\tools\regression_analyze_vs_legacy.py"     # 分析层与老 load_analyze 逐值一致
& $py "$dst\tools\test_face_pipeline.py"               # 表情识别（需要 tensorflow，约 10 秒）
& $py "$dst\tools\screenshot_ui.py"                    # 界面上线前的自检：六页离屏截图到 实验结果/_ui_shots
& $py "$dst\tools\test_camera_pipeline.py"             # 摄像头实时监测（假摄像头，不需要硬件）
```

分析层测试用 `tools/synth_session.py` 现造会话库（PQRST 合成心电 + 正弦瞳孔），
秒级跑完，不用等采集器实时攒数据。

界面：`python tools/run_ui.py --auto`（模拟源，起来就自动采集）；接真实设备加 `--real`。
摄像头实时监测：界面「表情」页 → 「打开摄像头」（会话在跑时帧会同时落盘 + 进 data_video_frame 表；
没会话就只监测不落盘）。识别仍在子进程里跑，实测约 130 ms/帧。

## 3. 必须遵守的约定

### 命名（按数据流分层，不按连接方式）

| 前缀 | 层 | 说明 |
|---|---|---|
| `A0_data_collect_*` / `core/A0_data_collect/` | 采集 | 设备 -> DataPacket |
| `A1_data_store_*` | 存储 | DataPacket -> SQLite / 文件 |
| `A2_analyze_*` | 分析 | 加工与特征计算 |
| `A3_ui_*` | 界面 | 新壳 |
| `A4_alert_*` | 告警 | 阈值判定 + 音频播报 |
| `A5_link_*` | 对外链路 | 推理机双向报文、采集卡主机遥控 |
| `A9_util_*` | 工具 | 日志、路径、配置 |

### 兼容契约（改之前务必看 README「兼容契约」）

1. 表名、列名、**列顺序**与老库逐字一致（分析代码用 `SELECT *` + `row[2]`/`row[3]`）。
2. `data_ecg.value` 必须是 JSON（`json.dumps({"ECG": [...], "send_time": "..."})`），
   不能是 Python repr —— 老代码有些版本写了 repr，分析端 `json.loads` 会炸。
3. `data_udp.value` 是 SmartEye 字典 + `send_time` 的 JSON，以 bytes 存 BLOB。
4. `created_date` 格式 `%Y-%m-%d %H:%M:%S.%f`。
5. 会话目录：`实验结果/<用户名>_<任务>_<YYYYmmdd>/normal_result/database.db`。
6. **握力按方案二**：`data_grip_raw` 存原始 18 通道；`data_grip_l/r` 由
   `A2_analyze_grip` 加工后写入（卡尔曼 -> 角度解算 -> 压力多项式拟合）。

### 握力算法参数（A2，改前先跑回归）

卡尔曼 `process_noise=0.01 / measurement_noise=2.0`；角度 ÷10、死区 0.2、±3600 回绕；
压力 ÷100 后代入四次多项式，末项偏移 L:-28 / R:-51；合成力
`p10*3 + p11*4 + p13*3 + p12 + p14*4`。

## 4. 待办（按建议优先级）

1. **A5_link：ctrl**（老 `collect_control_thread.py`）——SSH 登录采集卡主机
   （`[collect] ip`，rock/rock），做三件事：`sudo date -s` 对时、启动 `app_collect`、`pkill` 停止。
   老代码密码/路径全硬编码，需要提到配置。
2. **A4_alert：alert**（老 `alert.py`）——TCP 收单个 0/1 标志；最近 3 个都是 1 -> 高级警报；
   最近 5 个里 1 的个数在 2~4 -> 中级；不足 5 个不报；播
   `异常状态预警音/<纯语音|混合>/<内容>-<等级>.wav`（pygame）。
   注意：老代码 `alert_act` 表建了但**从没写过**（`alert_w` 零调用），新版应补上。
3. **A5_link：gpu**（老 `data_gpu.py`）——与推理机的双向报文（`struct.pack('q')` 长度头、
   图片 `128sq` 文件头）。**半成品**：老代码里真实推理线程被注释，跑的是 `test.py` 模拟数据。
   动手前先定义清楚"发什么、收什么"。
4. ~~**A2 其余**~~ **已完成**（2026-09-15）：ECG 特征、眼动窗口特征、负荷模型、表情识别都搬到
   `core/A2_analyze/`，回归测试与老实现逐值一致。**唯一没搬的是 `eye_analyze/eye_analyze.py`
   那套注视/扫视聚类**——它读的是 Tobii 字段（左/右眼注视点、睁眼度），而新采集器出的是
   SmartEye 报文，字段对不上，硬搬只会得到空表。要补这块得先定用哪些 SmartEye 字段
   （Saccade/Fixation 标志、EyelidOpening、GazeHeading/Pitch）和阈值口径。
5. ~~**A3 界面**~~ **已完成**（2026-09-15）：`core/A3_ui/` 用 PyQt5 + pyqtgraph 重写，
   不再有 2000 行的大类、不再有 MDI 子窗口、不再靠 UDP 8008 回环。启动入口 `tools/run_ui.py`。

## 5. 环境与工具

- 测试用的解释器：`F:\hcpsproject\PyCharmMiscProject\.venv\Scripts\python.exe`（借用老工程 venv）。
- Qt Designer 5.15.2：`F:\hcpsproject\qtdesigner\Lib\site-packages\qt5_applications\Qt\bin\designer.exe`，
  桌面有快捷方式「Qt Designer」。
- 老工程已修的历史问题：`config.ini` 由 GBK 转 UTF-8、读写统一 utf-8；
  `.venv` 的 `pyvenv.cfg`/activate 脚本/52 个 console exe 的 shebang 已指向新路径；
  `pyuic5.exe/pyrcc5.exe/pylupdate5.exe` 的 shebang 指向别的电脑（`C:\Users\mairui\...`），
  仍未修，需要时用 `python -m PyQt5.uic.pyuic` 代替。

## 6. 遗留杂项

- `F:\hcpsproject\HcpsApp\` 空壳目录（PyCharm 锁着删不掉，需先关 PyCharm）。
- `AAA_HcpsApp\core\__pycache__\.idea\`：曾经把 `__pycache__` 当项目打开的残留。
- 老工程根目录有 `.git`，`AAA_HcpsApp` 会被当成未跟踪文件（要不要 ignore 由你定）。
- 老工程里零引用的可丢弃物：`data_422.py`+`com/com_serial.py`（配置段不存在）、
  MQTT 配置（无 paho 代码）、根目录重复的 `ecg_process.py`/`load_assessment.py`、
  `window.py`、`save.py`、`performace_analyze/`。

## 7. 读老代码时挖到的坑（影响设计，别踩）

- 握力数据**从未进入负荷模型**（10 维负荷特征全是眼动+心电），`Synthesis_F` 没被用上。
- 老 `com/db.py` 的 `data_grip_r_w()` 写的是 `Data_grip_l` —— 右手数据进左手表。
- 老 `com/db.py` 的 `log_info()` 里 `is_debug == 'true'` 恒为 False（`is_debug` 已是 bool）。
- 老 `script.py` 里还有硬编码 `D:\pythonpro\...` 路径（旧机器）。
- 老 `alert.py` 的报警判定写在 TCP 接收线程里；`data_grip.py` 的卡尔曼写在串口线程里 ——
  新版一律把加工挪到 A2。
- **搬分析层时新挖到的坑（2026-09-15）**：
  - `ecg_process.py` / `load_assessment.py` 在**根目录和 `load_analyze/` 各有一份且内容不同**；
    界面用的是 `load_analyze/` 那份（`from load_analyze.ecg_process import get_input`），
    做回归对着它比。根目录那两份是零引用重复件。
  - 老 `_wavelet_denoise` 里软阈值算了 `threshold` 却**没写回系数**，细节层其实没阈值化；
    这是「口径」不是 bug，新版原样保留，改了负荷模型的输入就变了。
  - 心电时间轴用 `pd.date_range(首, 末, periods=样本数)` 均匀铺开，不是真实采样时刻；同样保留。
  - 等间距的合成心电会算出 `lf/hf = 0/0 = NaN`，SVM 不吃 NaN 会静默跳过；造数时要给心律不齐。
  - 会话目录带中文（`实验结果/`），**Windows 下 `cv2.imread/imwrite` 直接返回 None**；
    老工程靠 `face_model/read.py` 拷到临时目录绕开，新版统一用 `imdecode/imencode + np.fromfile/tofile`。
- **做界面时挖到的坑（2026-09-15）**：
  - **跨线程写控件 = 访问违例**：A0 采集器在自己的线程里打日志，一开始我把 logging 直接接到
    `QTextEdit` 上，结果偶发 `0x0000000000000008 内存不能为 read`（0xC0000005）——那个 0x8 就是
    "C++ 对象已经销毁还去读它的成员"。解法：日志也走 `LogBridge`（Qt 信号），跨线程自动排队到 GUI 线程。
    采集数据本来就是走 `PacketBridge` 信号，同一套路。
  - **离屏渲染（QT_QPA_PLATFORM=offscreen）时 Qt 字体库是空的**（`families()` 返回 0 个），
    界面能画出来但一个字都不显示；`theme.load_fonts()` 用 `addApplicationFont` 把
    `C:\Windows\Fonts\msyh.ttc` 等注册进去就好了（桌面环境本来就加载好了，多注册无害）。
  - **TensorFlow 与 PyQt5 同进程在 Windows 上有概率崩**，所以表情识别走独立子进程
    （`tools/face_cli.py --serve`，界面用 QProcess 驱动），界面进程不 import tf。
  - **hrvanalysis 对不理想的 RR 序列会 IndexError**（心搏太少、开头就是 NaN）：
    `interpolate_nan_values` 里 `while np.isnan(...[start_idx])` 会越界。老代码在这里直接崩，
    新版在 `A2_analyze/ecg.py` 里判最少心搏数并兜底，坏窗口跳过（正常数据结果不变，回归测试盯着）。
  - 老 `script.py` 的界面里没有"科技感"这种可复用资产，能复用的只有校徽和实验室照片（已放 `A3_ui/assets/`）。
  - **摄像头链路**：`A0_data_collect/camera.py` 用 cv2 直读（不走 gstreamer，实时监测要的是"立刻拿到这一帧"），
    帧的 JPEG 放在 `packet.raw` 里，界面直接解码预览，表情识别也直接吃这帧（子进程协议加了
    `{"id": n, "b64": "<JPEG base64>"}` 这种整帧请求）。每帧的亮度统计放在 `packet.meta`，
    界面据此提示"画面全黑"。
  - **这台机器上摄像头不可用**：`Get-PnpDevice` 里没有相机类设备，cv2 打开 0 号能成功但每帧
    全黑（均值 0、标准差 2），dshow/msmf/any 三种后端都试过。这是设备/隐私层面的问题
    （隐私挡板、被 WeChat/Edge 占用、或远程会话里没有摄像头），不是代码问题——
    换到有摄像头的机器上直接就能用，自检用 `tools/test_camera_pipeline.py`（假摄像头）。
