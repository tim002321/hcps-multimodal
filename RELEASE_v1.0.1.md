# v1.0.1 更新告示

发布日期：2026-11-23　　上一版本：v1.0.0（2026-11-21）

**一句话**：调试工具从主程序目录里搬出去了，主程序目录不再包含任何 `tools/`；同时补上了协作与安全的基线文件。

---

## 一、这次改了什么

### 1. 目录结构调整（会影响你手上的命令）

| 原来 | 现在 |
|---|---|
| `AAAwindows_code/tools/ble/...` | `debug_tools/ble/...` |
| `AAAwindows_code/tools/eye/...` | `debug_tools/eye/...` |
| `AAAwindows_code/tools/db/...` | `debug_tools/db/...` |
| `AAAwindows_code/tools/camera/...` | `debug_tools/camera/...` |
| `AAAwindows_code/tools/vision/...` | `debug_tools/vision/...` |
| `AAAwindows_code/receive_tcp_5001.py` | `debug_tools/eye/receive_tcp_5001.py` |

`debug_tools/` 与 `AAAwindows_code/` **同级**，位于仓库根目录：

```
PyCharmMiscProject/
├── AAAwindows_code/     客户版主程序（不含 tools/）
├── debug_tools/         内部排障工具（不参与打包）
├── eye_build/           眼动工具箱打包工程
├── face_tool/           面部 ROI 预处理工具
├── scripts/             通用脚本（md2pdf 等）
└── 需求与产品化规划.md
```

移动用 `git mv` 完成，**文件历史保留**，`git log --follow` 仍可查到每个脚本的完整提交记录。

**你需要注意的**：以前习惯敲的命令要换路径。例如

```bash
# 旧
python AAAwindows_code\tools\ble\diag_ecg.py --name E1220511000212
# 新
python debug_tools\ble\diag_ecg.py --name E1220511000212
```

脚本内部的路径推导已同步改为「上溯到仓库根再拼 `AAAwindows_code`」，所以**整个仓库搬盘、换目录都不用改代码**。

### 2. 新增文件

| 文件 | 作用 |
|---|---|
| `debug_tools/README.md` | 工具清单、路径约定、常用命令、新增工具的写法要求 |
| `.gitattributes` | 统一换行 `eol=lf`，二进制显式标记 |
| `CONTRIBUTING.md` | 分支命名、提交格式、禁止提交清单、冲突高发文件处理约定 |

### 3. 安全清理（配合仓库转为公开）

| 位置 | 处理 |
|---|---|
| `AAA_HcpsApp/config/default.yaml` | SSH 用户名与口令清空，改为从 `HCP_SSH_HOST/USER/PASSWORD` 环境变量读取 |
| 两份代码审查报告、`HANDOVER.md` | 引用凭据与内网 IP 的 4 处改为脱敏表述 |
| `face_tool/README.md`、`run_cli.py` | 示例路径中的真实姓名改为 `sub-014` |
| `face_tool/tests/test_session.py` | 样本路径改由 `HCP_SAMPLE_SESSION` 环境变量提供，未设置时该组用例自动跳过 |
| `.gitignore` | 新增 `mental_math_passcode.txt`、`*.pem`、`*.key`、`.env*`、`_pdfsrc/` |

### 4. 修复

`ecg_probe_gui.py`：会话级统计跨重连不再清零，掉线日志补记持续时长。此前只显示最后一次连接的数据，会把「先丢包后掉线」误判成「零丢包突然断开」。

---

## 二、升级步骤

```bash
cd F:\hcpsproject\PyCharmMiscProject
git pull
git switch main
git pull
```

因为新增了 `.gitattributes`，**第一次更新后请执行一次**换行归一化，否则本地 diff 会显示大量「整文件改动」：

```bash
git add --renormalize .
git commit -m "chore: 统一换行符为 LF"
```

确认版本：

```bash
type AAAwindows_code\VERSION      # 应显示 1.0.1
git describe --tags                # 应显示 v1.0.1
```

---

## 三、兼容性影响

| 项 | 是否受影响 |
|---|---|
| 主程序 `HcpsMonitor.exe` | **不受影响**。本次没有改任何采集、存储、界面代码，`config.ini`、数据库结构、会话目录格式全部不变 |
| 已采集的会话数据 | 不受影响，仍可直接回放与分析 |
| 调试工具的调用命令 | **受影响**，路径前缀由 `AAAwindows_code\tools\` 改为 `debug_tools\` |
| `EyeToolbox.exe` 打包工程 | 需要重新打包才会用新路径；已打包的旧 exe 仍可运行（它自带 `_internal`，不依赖仓库目录） |
| `face_tool` | 不受影响，仅文档与测试里的姓名做了匿名化 |

---

## 四、验证记录

| 检查 | 结果 |
|---|---|
| 全部脚本编译 | 通过，无语法错误 |
| `receive_tcp_5001.py --selftest` | 8/8 通过 |
| `test_eye_protocol.py` | 42/42 通过 |
| `test_yawn_pipeline.py` | 23/23 通过 |
| `fetch_models.py --check` | 正确定位 `AAAwindows_code/models`，两个权重校验和一致 |
| `diag_ecg.py --scan 3` | 正确读取 `config.ini` 中 15 个设备名，扫描流程正常 |
| `eye_toolkit_gui.py` | 可正常构建界面，`receive_tcp_5001` 从新位置导入，`BASE` 解析为 `AAAwindows_code` |

---

## 五、已知遗留

1. **旧凭据仍在 Git 历史中**。`5fdf4d5` 提交里的 `user: rock / password: rock` 已推到公开仓库，清除需要重写历史并 force push。**真正的修复是轮换那台采集机（192.168.1.x）的 SSH 口令**，仅改文件不解决暴露问题。
2. **`EyeToolbox.exe` 未重新打包**。打包工程已指向新路径，但产物仍是旧版本；需要时执行 `eye_build` 下的 PyInstaller 命令。
3. **`AAAwindows_code/docs/重构说明.md` 中的 `tools/` 路径未改**。那是描述 2026-10 那次重构的历史文档，改动会与当时事实不符，按原样保留。
4. **模型权重仍在历史中**（`cnn3_best_weights.h5` 82 MB）。单次 clone 需拉 84 MB，暂不处理。

---

## 六、下一步（按优先级）

| 优先级 | 事项 | 对应编号 |
|---|---|---|
| P0 | 心电贴连接稳定性：现场取证 → 补超时与退避 → 模拟信号兜底 | U1 / R8 |
| P0 | 连续运行：会话切换不重启、异常退出可识别 | U4 / R1 / R13 |
| P1 | 启动画面、分析进度条、心电短窗刷新、PPG 合成显示 | R5 / R6 / R7 / R9 |
| P1 | 疲劳模型（先规则后模型，前置接入 68 点） | U2 / R10 |

详细验收标准见 `需求与产品化规划.md` 与项目空间中的同名 PDF。
