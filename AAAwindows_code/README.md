# 蓝牙生理数据采集程序（Windows 11）

在 Windows 11 上位机上直接接收**心电贴 + 绿光手环 + USB 摄像头**三路数据并统一入库，
不再需要 Linux 盒子转发。

- 心电/手环走 BLE（`bleak`，WinRT 后端，替代原 Linux 版的 `bluepy`）
- 摄像头走 OpenCV，按目标帧率（默认 30fps）截帧、JPEG 入库
- 三路数据写进**同一个会话库**，时间轴天然对齐
- 提供纯采集入口 `main.py` 与实时监测界面 `run_monitor.py`

> 先读 **[docs/架构说明.md](docs/架构说明.md)** 了解数据流与分层；
> 想了解这次目录调整改了什么，看 **[docs/重构说明.md](docs/重构说明.md)**。

## 目录结构

```
AAAwindows_code/
├── main.py                采集入口（无界面：只写库 / 可选写 JSONL）
├── run_monitor.py         采集 + 实时监测界面
├── config.ini             设备名、扫描参数、存储参数
├── requirements.txt
├── core/                  核心层（不依赖设备与界面）
│   ├── config.py          config.ini → Config
│   ├── db_storage.py      SQLite 存储（SensorDB / DataRecorder）
│   ├── writer_thread.py   写库线程（把解析入库挪出 BLE 回调）
│   └── device_status.py   线程安全的设备状态表
├── devices/               设备层
│   ├── ecg_receiver.py    心电贴（BLE → 244 字节包解码）
│   ├── bracelet_protocol.py 手环协议解析（纯字节，可离线测）
│   ├── bracelet_receiver.py 手环接收（BLE）
│   ├── camera_receiver.py USB 摄像头采集
│   ├── ble_utils.py       BLE 公共工具（扫描 / 服务表打印）
│   └── legacy_bracelet.py 手环协议独立调试脚本（主程序不引用）
├── ui/                    界面层
│   ├── base.py            页面基类 + 曲线网格
│   ├── buffers.py         实时缓冲 + 入库钩子
│   ├── replay.py          历史库 / JSONL 回放
│   ├── pages_extra.py     会话与回放 / 状态识别 / 数据分析 / 预留设备页
│   ├── profile.py         登录与被试信息（人物名 / 组别 / 实验时间 / 总线 IP）
│   ├── window.py          主窗口 + 两级导航 + 采集侧六页
│   ├── widgets.py theme.py 组件与配色
│   └── assets/
├── analysis/              分析层（纯 numpy，可离线跑）
│   └── features.py        HRV / PPG 脉搏特征（算不出来返回 n/a + 原因）
├── tools/                 排障与自检
│   ├── ble/               扫描、扫描重试策略验证
│   ├── camera/            摄像头枚举、帧导出
│   ├── db/                库概览、合并、回放检查、同库验证
│   └── ui_screenshot.py   离屏截图 + 界面冒烟断言
├── docs/                  架构说明 / 需求拆解 / 重构说明 / 手环协议文档
├── data/                  运行时生成的会话库
└── ui_shots/              界面截图
```

## 界面导航

侧边栏两级，一级是工作阶段：

| 一级 | 二级 |
|---|---|
| 采集 | 设备准备 / 心电 / 手环 / 摄像头 / 肌电（预留）/ 眼动（预留） |
| 数据查看 | 会话与回放（可回放，也可删除会话库） |
| 状态识别 | 实时结果（四个模型，待接入） |
| 数据分析 | 区间分析（勾选时间段 → 特征提取 → 导出 CSV） |

启动 `run_monitor.py` 会先弹「实验信息录入」：人物名 / 组别 / 实验时间决定会话目录
`data/<组别>/<人物名>_<时间>/`，界面上的「总线 IP」用于后续上传（`config.ini [server]`）。

## 环境要求

- Windows 10 / 11（已启用蓝牙并插入蓝牙适配器；摄像头可选）
- Python 3.8+（建议 3.10+）

## 安装

```powershell
cd AAAwindows_code
pip install -r requirements.txt
```

## 运行

```powershell
# 纯采集（只写库，可选写 JSONL）
python main.py

# 带界面实时监测
python run_monitor.py
```

常用参数（两个入口都支持）：

```powershell
# 指定心电设备名（多个用英文逗号分隔）
python main.py --device E1220511000413,E1220511000444

# 只要心电，不连手环
python main.py --bracelet ""

# 扫描 15 秒
python main.py --scan 15

# 连接后打印服务/特征表（排查 handle 时使用）
python main.py --debug

# 不写 JSONL 文件（只入库）
python main.py --out none
```

也可以在 `config.ini` 中配置默认值。

> 控制台只打印「连接成功」「设备掉线」两类日志，采集数据不再打印。


## 工作流程

1. 扫描蓝牙广播，按广播名称匹配目标心电设备；
2. 连接后按原协议下发握手命令：
   - handle `20`、`25` 写 `0x0100`（使能）；
   - handle `22` 依次写“结束采集”、“开始采集”命令；
3. 订阅服务 `554b0001-15e1-4af1-a6e3-588089f20b68` 的第一个特征的通知；
4. 收到 244 字节数据包后解码，输出 JSON，并可选写入 JSONL 文件；
5. 断开后自动重连。

## 可视化界面（run_monitor.py）

```powershell
python run_monitor.py                          # 真实蓝牙，读 config.ini
python run_monitor.py --device E1220511000413  # 覆盖心电设备名
python run_monitor.py --replay data/20261007_145730.db --speed 2   # 回放历史数据
python run_monitor.py --no-ble                 # 只开界面，稍后在「状态」页选文件回放
```

界面是三层结构（浅色工业风，参照现场数据监控界面）：

| 位置 | 内容 |
|---|---|
| 左侧 | 蓝色块导航：心电 / 手环 / 状态 |
| 顶部 | 当前页标题 + 数据源 + 时钟；下面一排数值块（心率/体温/呼吸率/丢包 或 PPG 速率/GSR/肤温/电量） |
| 中间 | 2 列黑色曲线网格：参数名 + 实时值，黑底、彩色曲线、黄色竖向游标 |
| 右侧 | 「曲线显示参数」：待选参数 / 已选参数 双表（序号/参数名/单位）+ 添加/删除，可随时增减曲线 |
| 底部 | 「清空曲线」+ 当前包数/采样率/连接状态 |

### 设备准备（第一页）

启动后先到「00 设备准备」：点「开始连接」会按顺序做

1. 扫描蓝牙广播（心电贴 + 绿光手环）
2. 连接心电贴（bleak，握手后订阅通知）
3. 连接绿光手环（自动发现通知特征）
4. 打开 USB 摄像头（cv2，默认 640×480 @30fps）
5. 三个设备都 `connected` 后显示绿色「设备已全部连接，可以开始采集了」

三张卡片实时显示设备状态：未开始 / 扫描中 / 连接中 / **已连接** / 已掉线 / 连接失败，
下面一条条列出连接流程的进度；「重新扫描」会断开重连，「全部断开」复位。
连接状态由 `core/device_status.py` 的 `DeviceStatus` 记录（BLE 线程写、界面线程读），
接收模块新增 `status=` 参数上报 `connecting/connected/lost/error`，界面不解析日志。

**扫描策略**：不会扫一次没找到就报失败。心电贴和手环各自连续扫描
`scan_attempts` 次（`config.ini` 默认 10 次，每次 `scan_timeout` 秒）：

- 第 1~10 次：状态显示「扫描中 · 第 k/10 次扫描」；
- 连续 10 次都没扫到：才显示「未找到 · 已连续 10 次扫描未找到，仍在重试…」，
  后台继续按间隔重试，设备开机后会自动连上。

验证这条逻辑（不需要设备）：

```powershell
python tools/ble/ble_retry_test.py
```

启动时想直接连设备：`python run_monitor.py --auto-connect`。

三个页面：

* **心电**：ECG、呼吸、三轴 X/Y/Z、体温；心率用峰值法实时估计，**信号过弱/饱和时显示 `--`**
  （实测发现电极没贴时设备仍按 250Hz 推 ±1 的噪声，硬算会报出 190+ 的假心率）。
* **手环**：4 通道 PPG（自动做交流耦合，否则 24bit 原始值的直流分量有上百万）、GSR、腕部三轴。
* **状态**：设备连接状态、包数/采样点统计、回放控制（进度条 + 回放预览曲线）。
  回放区上方是文件选择/倍速/开始-停止，中间是进度条和"第 k/N 包"，下方两块黑底曲线
  实时显示正在回放的心电与 PPG 通道 1，右侧还有一块**摄像头回放画面**
  （回放库里没有帧时自动切到当前摄像头实时画面）。

## USB 摄像头（30fps 入库）

界面第 3 页「摄像头」：选设备号/分辨率/目标帧率/JPEG 质量 → 「开始采集」，
左边实时预览，右边是设置；顶部显示**已入库帧 / 库大小**。
页面右上角有**「开始采集」/「结束采集」**两个按钮（每个页面都有）：
开始 = 扫描蓝牙 + 打开摄像头，结束 = 断开全部设备（设备在跑时开始键置灰，反之亦然）。

也可以不开界面单独采：

```powershell
python -m devices.camera_receiver --index 0 --seconds 10 --fps 30
python -m devices.camera_receiver --index 0 --width 1280 --height 720 --quality 80
```

**数据库**：摄像头帧和心电/手环数据**写在同一个会话库**（`data/YYYYmmdd_HHMMSS.db`），
只是表不同，回放时一起读，时间轴天然对齐。

```sql
CREATE TABLE camera_frames (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    time TEXT NOT NULL,            -- %Y-%m-%d %H:%M:%S.%f
    camera TEXT,
    frame_index INTEGER NOT NULL,  -- 从 1 递增
    width INTEGER, height INTEGER,
    jpeg BLOB NOT NULL             -- 原始 JPEG 字节，cv2.imdecode 即可
);
CREATE TABLE camera_sessions (
    id, started_at, ended_at, camera, width, height, target_fps, frame_count
);
```

**帧率控制**：摄像头若比目标帧率快（例如 60fps），超出的帧直接跳过；
采样按固定节拍推进，不会把 JPEG 编码耗时算进周期（否则实测只有 20fps）。

**给分析用**：导出入库的帧

```powershell
python tools/camera/export_camera_frames.py data/camera_20261017_143135.db
python tools/camera/export_camera_frames.py data/camera_xxx.db --out frames --every 30   # 每秒取 1 张
```

> 存储估算：640×480 / JPEG80 ≈ 25~40KB/帧 → 30fps ≈ 0.8~1.2MB/s（约 3~4GB/小时）；
> 1280×720 约翻倍。长时间录制建议调低质量或改成「存文件 + 库里存路径」。

验证两类数据确实同库（不需要硬件）：

```powershell
python tools/db/test_shared_db.py
```

没有硬件时用 `--replay` 或「状态」页的回放面板；截图自检：

```powershell
python tools/ui_screenshot.py     # 用历史数据填满三页并离屏截图到 ui_shots/
```

> 注意：`data/` 里现有的几段录音，心电通道大部分时间是 ±1 的底噪（电极未接触），
> 呼吸通道固定上报 0，PPG 是原始 24bit 值。界面会如实显示并在信号不可用时给出提示，
> 接上电极后重新采一段即可看到真实波形。

## 数据包格式（244 字节）

| 偏移 | 字段 |
| --- | --- |
| 0 | 包头 `0x5B` |
| 1 | 指令类型 |
| 2 | 包长度 |
| 3 | 未知 |
| 4:8 | 包号（uint32 大端） |
| 8 | 类型字1（`0x01` = ECG） |
| 9 | 类型1长度 |
| 10 | loff |
| 11:13 | 体温（int16 小端）/ 100.0 |
| 13:173 | 80 个 ECG 采样点（int16 小端，有符号） |
| 173 | 类型字2（`0x03` = 呼吸） |
| 174 | 类型2长度 |
| 175:191 | 8 个呼吸采样点（int16 小端，有符号） |
| 191 | 类型字3（`0x02` = 三轴） |
| 192 | 类型3长度（字节数，÷3 为采样点数） |
| 193:209 | x 轴（int8）/ 64.0 |
| 209:225 | y 轴（int8）/ 64.0 |
| 225:241 | z 轴（int8）/ 64.0 |
| 241:243 | CRC16 |
| 243 | 包尾 `0x5D` |

## 输出示例

```json
{
  "time": "2026-09-02 10:00:00.123456",
  "device": "AA:BB:CC:DD:EE:FF",
  "data": {
    "ECG": {"体温": 36.5, "ECG": [1024, 1025, ...]},
    "呼吸": [100, 101, ...],
    "三轴": [{"x": 0.0, "y": 0.0, "z": 1.0}, ...],
    "_包号": 12345,
    "_CRC16": "1A2B"
  }
}
```

## 常见问题

- **扫描不到设备**：确认设备已开机并广播、蓝牙已开启；`--debug` 模式可打印服务表帮助排查。
- **连接后无数据**：用 `--debug` 查看服务/特征表，确认 handle `20/22/25` 对应关系是否与原设备一致。
- **数据乱码/丢包**：程序已内置缓冲重组逻辑，可应对 MTU 不足导致的分包。
