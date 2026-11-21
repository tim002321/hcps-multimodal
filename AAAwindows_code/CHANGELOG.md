# CHANGELOG — AAAwindows_code

本文件记录**基线之后**的变化。基线为 `dist/HcpsMonitor/HcpsMonitor.exe`（构建于 2026-11-15 15:55，
对应源码状态，zip 封装于 2026-11-17 09:46）。

只记录对源码、配置、文档的实质改动；运行期产生的 `data/`、`dist/`、`build/` 不在范围内。

---

## 2026-11-21

### 新增：心电贴诊断与检测工具（独立运行，不改主程序）

| 文件 | 用途 |
|---|---|
| `tools/ble/diag_ecg.py` | 六阶段分级诊断：扫描 → 建链 → 服务发现 → 定位特征 → 下发命令 → 统计收包。每阶段独立超时，用于定位「一直显示正在连接」卡在哪一步 |
| `tools/ble/ecg_probe_gui.py` | Tk 图形检测工具：实时波形、包速率、丢包率、体温、MTU、逐次掉线记录、指数退避自动重连；支持 `--auto` 自动连、`--demo` 合成数据自检 |

**实测结论（E1220511000212，固件 1.3.1_rougu_r3）**

- 全链路可通：MTU 247，单包 244 字节，约 3.1 包/秒（250 Hz × 80 点/包）
- 第 4 字节起的包号实际是 **uint16 小端**（实测 245 → 271，差 26，收 27 包，0 丢包）
- offset 10 的状态字节在贴身前后恒为 `0x80`，**不是电极脱落标志**；判断贴合与否应看体温与峰峰值
- 刚贴上后 30～60 秒是放大器基线恢复期，峰峰值可达 6 万（满量程回绕），属正常现象

---

## 2026-11-17

### 新增：眼动独立采集工具链（不接入主程序）

| 文件 | 用途 |
|---|---|
| `receive_tcp_5001.py` | 眼动数据独立采集，客户端模式连接 `192.168.2.98:7901`，支持 26 键 JSON 报文 |
| `tools/eye/eye_live_gui.py` | 实时入库界面：目录探测、按数据增长判定会话、迟滞锁定、grace 拦截 |
| `tools/eye/import_eye_csv.py` | CSV → 会话库 `eye_data`，按整帧去重、幂等写入、支持传会话目录自动配对 |
| `tools/eye/replay_eye_csv.py` | CSV 按原节奏回放成 TCP 流，用于离线验证 |
| `tools/eye/eye_toolkit_gui.py` | 单窗口四页签工具箱（实时入库 / 录制 CSV / 导入到库 / 回放测试） |
| `tools/eye/.eye_live_gui.json` | 工具箱设置：设备地址、端口、默认数据目录 |

关键行为：CSV 默认勾选并自动命名（`眼动数据_YYYYMMDD_HHMMSS.csv`），写入走独立线程，
队列满时丢帧并计数；同名文件追加、不覆盖。

### 已回滚：主程序眼动 TCP 接入

曾尝试把眼动数据直接接入主程序采集流程，随后按要求**完整回滚**，主程序恢复为打包时的状态。

回滚涉及文件（当前内容已与「含 TCP 版本」的备份不同，TCP 痕迹为 0）：

```
ui/window.py             ui/pages_extra.py      run_monitor.py
core/config.py           config.ini
devices/eye_receiver.py  devices/eye_protocol.py
```

验证方式：`grep -ci tcp devices/eye_receiver.py ui/window.py` 结果均为 0；
打包期特征仍在位（`CAPTURE_FPS = 15.0`、`face_only=true`、`camera_videos` 建表、分析页「项目/数值」两列）。

### 文档

- `README.md`：补充眼动工具链的调用方式与数据口径说明

---

## 未纳入基线的独立产物

| 目录 | 说明 |
|---|---|
| `eye_build/` | EyeToolbox 单文件打包产物（约 28 MB），由 `tools/eye/` 构建，独立于主程序 |
| `face_tool/` | 面部 ROI 离线处理工具（独立程序，含 3.9 GB 打包产物），不属于主程序 |

---

## 已知问题（待修）

以下问题均位于 `devices/ecg_receiver.py`，尚未修改：

1. **包号宽度读错**：按 4 字节大端读（`>I`），实际为 uint16 小端，导致每包增量恒为 16,777,216，
   丢包统计完全不可用。
2. **硬编码 GATT 句柄失效**：`HANDLE_ENABLE_1/2`（20/25）、`HANDLE_DATA`（22）在该固件上均不存在，
   依赖回退匹配才能工作；回退失败时走静默 `continue`，界面永久停在「正在连接」。
3. **三处 await 无超时**：`client.services`、`write_gatt_char(response=True)`、`start_notify`，
   设备不回应时不会抛异常，状态既不转红也不打日志。
4. **重连策略**：固定 3 秒、不区分失败类型、不重新扫描设备对象、无退避。
5. **多设备混数据**：`ecg_data` 表只有 `time` / `value` 两列，无设备列；配置里 14 个设备名任一匹配
   即建连接任务，多片同时开机时数据会混进同一张表且无法区分。

---

## 说明

基线之后的改动尚未提交到版本库（仓库 HEAD 停在 2026-11-03），本文件作为第一份提交引入。
