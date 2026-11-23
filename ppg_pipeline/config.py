# -*- coding: utf-8 -*-
"""
配置模块：绿光手环四通道 PPG 分析 —— 路径、采样率与信号处理参数。
所有可调参数集中在此，方便复现与调整。
"""
import os

# ---------------------------------------------------------------- 路径
# 原作者的默认路径（D:\projects\...）。本机由环境变量覆盖：
#   PPG_DB   指向采集工程产生的会话库（bracelet_ppg_data / bracelet_accel_data / ecg_data）
#   PPG_OUT  结果输出目录
PROJECT_DIR = os.environ.get("PPG_PROJECT_DIR") or r"D:\projects\试飞院二期\数据分析"
DB_PATH = os.environ.get("PPG_DB") or os.path.join(PROJECT_DIR, "朱-疲劳-14：24.db")
OUTPUT_DIR = os.environ.get("PPG_OUT") or os.path.join(PROJECT_DIR, "ppg_analysis", "output")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ---------------------------------------------------------------- 采样率
# 由 data_loader 从数据跨度与行数自动估计；此处为回退默认值
FS_PPG_FALLBACK = 250.0   # 4 通道 PPG
FS_ACC_FALLBACK = 25.0    # 三轴加速度
FS_ECG_FALLBACK = 250.0   # ECG 参考

# ---------------------------------------------------------------- 滤波器参数
FILTER = dict(
    base_line_hp=0.5,      # 去基线高通截止(Hz)
    band_low=0.5,          # 带通低截止(Hz)
    band_high=8.0,         # 带通高截止(Hz)
    order=4,               # Butterworth 阶数
    freq_tol=1e-9,         # 避免除零
)

# ---------------------------------------------------------------- 运动伪影去除(NLMS 自适应对消)
ARTIFACT = dict(
    ref_order=4,           # 每个加速度轴使用的自适应滤波器抽头数(参考信号阶数)
    mu=0.01,               # NLMS 步长
    eps=1e-9,              # 正则化
    motion_band_low=0.3,   # 运动强度包络带通低截止(Hz)
    motion_band_high=3.0,  # 运动强度包络带通高截止(Hz)
    motion_thresh_std=1.5, # 运动掩码阈值(运动强度超过均值+k*std 视为运动段)
)

# ---------------------------------------------------------------- 异常点检查
OUTLIER = dict(
    mad_k=10.0,            # 差分域阈值系数(相对差分 MAD)
    diff_floor_pct=99.0,   # 差分阈值下限百分位(只抓显著尖峰)
    amp_k=10.0,            # 长窗幅度离群阈值系数
    win_sec=0.4,           # 短窗(差分辅助)
    amp_win_sec=2.0,       # 长窗(幅度离群)
)

# ---------------------------------------------------------------- 脉搏峰检测
PEAK = dict(
    min_hr=40,             # 期望最低心率(bpm)，用于最小峰间距
    max_hr=200,            # 期望最高心率(bpm)
    prominence_ratio=0.35, # 峰显著性阈值(相对信号振幅)
    peak_height_quantile=0.3,
    ppi_min=0.3,           # 有效 PPI 下限(s)
    ppi_max=1.5,           # 有效 PPI 上限(s)
)

# ---------------------------------------------------------------- 频域 PRV
PRV_FREQ = dict(
    resample_hz=4.0,       # PPI 插值重采样率(Hz)
    lf=(0.04, 0.15),       # 低频带(Hz)
    hf=(0.15, 0.40),       # 高频带(Hz)
)

# ---------------------------------------------------------------- 特征融合
ATTENTION = dict(
    temperature=1.0,       # 注意力 softmax 温度
)

# ---------------------------------------------------------------- 滑动窗口(特征时变输出)
WINDOW = dict(
    win_sec=300.0,         # 窗口长度(秒)=5 分钟
    step_sec=60.0,         # 滑动步长(秒)=1 分钟
)
