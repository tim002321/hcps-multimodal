# 协作说明（CONTRIBUTING）

本仓库为**公开仓库**，因此除代码规范外，还有两条硬约束：**不提交凭据**、**不提交被试可识别信息**。

## 一、谁能写代码

| 身份 | 权限 | 方式 |
|---|---|---|
| 仓库所有者 | 管理员 | 可直接推送 `main` |
| 协作者（collaborator） | 写权限 | 由所有者在 Settings → Collaborators 邀请，接受后可直接推送分支 |
| 其他人 | 无写权限 | Fork → 提交到自己仓库 → 向上游提 Pull Request |

公开仓库只代表「能看」，**不代表「能改」**。要直接推送，必须被邀请为协作者。

## 二、分支与提交

**分支命名**

```
feat/<简短描述>      新功能，例：feat/session-switch
fix/<简短描述>       修缺陷，例：fix/ecg-reconnect-backoff
docs/<简短描述>      仅文档
chore/<简短描述>     构建、依赖、忽略规则
```

**提交信息**（一行标题 + 可选正文）

```
<type>(<scope>): <说明>

type  : feat | fix | docs | refactor | perf | test | chore
scope : ecg | bracelet | camera | eye | ui | db | tools | face | docs
```

例：

```
fix(ecg): 重连改为指数退避，稳定 10 秒才归零

原实现固定 3 秒重连，撞上 supervision timeout 窗口（4~6 秒），
每次必失败，且一夜可产生 2.8 万次连接尝试。
```

**推送前先同步**

```bash
git pull --rebase origin main     # 在自己的分支上
git push
```

两人以上同时改同一批文件时，走 分支 + Pull Request，`main` 上只保留可运行的状态。

## 三、绝对不要提交的内容

| 类别 | 具体 | 原因 |
|---|---|---|
| 凭据 | 口令、token、SSH 用户名/密码、API key | 公开仓库，一旦推送即视为泄漏，只能靠轮换补救 |
| 被试可识别信息 | 真实姓名、学号、手机号、人脸图像 | 违反知情同意与伦理要求 |
| 采集数据 | `data/`、`*.db`、`frames/`、`video/`、`*.csv` 眼动原始数据 | 体积大且含个人信息 |
| 模型权重 | `*.pt`、`*.onnx`、`*.dat`、`*.h5`、`*.cbm`、`*.pkl` | 单个几十 MB，把仓库撑大，克隆变慢 |
| 构建产物 | `dist/`、`build/`、`*.exe`、`*.zip` | 可重复生成 |

`.gitignore` 已覆盖上述大部分，但**不要用 `git add -A`**，逐目录确认后再提交。

提交前自检：

```bash
git status                        # 看清楚将要提交什么
git diff --cached --stat          # 文件数与行数是否合理
git grep -nE "password|token|secret" --cached   # 是否有凭据混入
```

## 四、两个冲突高发文件

**1) `AAAwindows_code/config.ini`**

`device_names` 是**一整行**逗号分隔的设备名，多人各自增删设备必然冲突，且合并时极易漏项。
约定：**改这一行前先在群里说一声**；合并冲突时以「两侧并集」为准，逐个人工核对，不要机械接受某一侧。

**2) `需求与产品化规划.md`**

正文是宽表格，一行表格在 Git 里就是一行文本，两人改相邻章节即冲突。
约定：同一时间只安排一人改这份文档；需要并行时按章拆文件（如 `需求-R.md`、`需求-S.md`）。

## 五、本地环境

```
Python 3.12，虚拟环境在项目根的 .venv/
主要依赖：PyQt5、opencv-python、bleak、ultralytics(torch)、dlib、pymupdf
```

**常用命令**

```bash
.venv\Scripts\python.exe AAAwindows_code\run_monitor.py          # 启动主程序
.venv\Scripts\python.exe face_tool\run_gui.py                     # 面部 ROI 工具
.venv\Scripts\python.exe face_tool\run_cli.py <会话目录> -o <输出>  # 命令行版
.venv\Scripts\python.exe -m unittest discover -s face_tool\tests -t face_tool   # 单元测试
.venv\Scripts\python.exe scripts\md2pdf.py 需求与产品化规划.md      # 文档转 PDF
```

样本会话相关的集成测试需要环境变量，否则自动跳过：

```bash
set HCP_SAMPLE_SESSION=F:\...\data\<被试>\<会话目录>
```

## 六、改动完成后的最低要求

1. 受影响的单元测试通过；界面类改动附一张截图
2. 在提交信息里写清**影响范围**（哪些设备、哪些页面、是否需要重采数据）
3. 涉及数据结构（新增表/列）时，同步更新 `CHANGELOG.md` 与读取端的兼容逻辑
4. 涉及版本发布时，更新 `AAAwindows_code/VERSION` 并打 tag（见 `版本管理.md`）
