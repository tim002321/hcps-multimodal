# -*- coding: utf-8 -*-
"""
单通道预处理模块。

顺序（严格对应任务要求）：
  1) 运动伪影去除：以三轴加速度为参考信号，构造参考矩阵(每轴多阶延迟)，
     用批量自适应最小二乘(LMS 收敛解)估计运动伪影并减除；同时输出运动强度
     掩码(带通加速度模超阈值)供信号质量评估使用。
  2) 去基线：高通滤波(默认 0.5 Hz)。
  3) 带通滤波：0.5–8 Hz，覆盖心率频带。
  4) 异常点检查：局部中值+残差 MAD 阈值，检测尖峰/阶跃，线性插值修复并统计。
"""
import numpy as np
from scipy import signal
import config as cfg


def _delay_matrix(x, order):
    """把信号 x 扩展成 order 个延迟副本列矩阵 (n, order)。"""
    n = len(x)
    cols = []
    for d in range(order):
        col = np.zeros(n)
        if d > 0:
            col[d:] = x[:-d]
        else:
            col[:] = x
        cols.append(col)
    return np.column_stack(cols)


def motion_artifact_removal(ppg, ax, ay, az, fs_ppg, fs_acc, params=None):
    """
    以三轴加速度为参考去除运动伪影。
    返回 (clean, motion_mask, motion_intensity, ref_weights)。
      clean           : 去除运动伪影后的 PPG
      motion_mask     : 运动段布尔掩码(与 PPG 同长)
      motion_intensity: 运动强度包络(带通加速度模)
      ref_weights     : 自适应参考权重(用于说明伪影来源结构)
    """
    p = params or {}
    order = p.get("ref_order", cfg.ARTIFACT["ref_order"])
    mu = p.get("mu", cfg.ARTIFACT["mu"])
    eps = p.get("eps", cfg.ARTIFACT["eps"])
    ml = p.get("motion_band_low", cfg.ARTIFACT["motion_band_low"])
    mh = p.get("motion_band_high", cfg.ARTIFACT["motion_band_high"])
    kth = p.get("motion_thresh_std", cfg.ARTIFACT["motion_thresh_std"])

    # ---- 1. 加速度上采样到 PPG 时间轴(线性插值) ----
    t_acc = np.arange(len(ax)) / fs_acc
    t_ppg = np.arange(len(ppg)) / fs_ppg
    ax_r = np.interp(t_ppg, t_acc, ax)
    ay_r = np.interp(t_ppg, t_acc, ay)
    az_r = np.interp(t_ppg, t_acc, az)

    # ---- 2. 构造参考矩阵：三轴各自多阶延迟 ----
    R = np.hstack([_delay_matrix(ax_r, order),
                   _delay_matrix(ay_r, order),
                   _delay_matrix(az_r, order)])
    nref = R.shape[1]

    # ---- 3. 最小二乘估计运动伪影并减除 ----
    # 原实现是"归一化 LMS 跑 40 次迭代"，步长 mu 与参考列的能量无关，
    # 在本机数据上（加速度 ~1 g，PPG ~1e6 counts）会振荡发散：权重跑到 1e5 量级，
    # 减出来的 artifact_est 幅度 ~1e6，反而把信号彻底搅烂。
    # 这里改成闭式最小二乘（等价于 LMS 收敛解，但不会发散）：
    # 参考列先 z-score 标准化，避免病态；权重再按列标准差换算回原始尺度。
    d = ppg.astype(np.float64).copy()
    Rc = R - R.mean(axis=0, keepdims=True)
    dm = d - d.mean()
    col_std = Rc.std(axis=0)
    col_std[col_std < eps] = 1.0
    Rs = Rc / col_std
    try:
        w_scaled, *_ = np.linalg.lstsq(Rs, dm, rcond=None)
    except np.linalg.LinAlgError:
        w_scaled = np.zeros(nref)
    w = w_scaled / col_std                       # 换算回原始参考尺度
    artifact_est = Rc @ w + d.mean()
    clean = d - artifact_est
    # 移除极端残余(可选幅度钳制由异常检查处理)

    # ---- 4. 运动强度掩码：加速度模的带通包络超阈值 ----
    mag = np.sqrt(ax_r ** 2 + ay_r ** 2 + az_r ** 2)
    b, a = signal.butter(4, [ml, mh], btype="bandpass", fs=fs_ppg)
    mf = signal.filtfilt(b, a, mag - mag.mean())
    thresh = mf.mean() + kth * mf.std()
    motion_mask = mf > thresh
    return clean, motion_mask, mf, w


def _filt(series, fs, ftype, low=None, high=None, order=4):
    """通用零相位 Butterworth 滤波。"""
    if ftype == "highpass":
        b, a = signal.butter(order, high, btype="highpass", fs=fs)
    elif ftype == "bandpass":
        b, a = signal.butter(order, [low, high], btype="bandpass", fs=fs)
    return signal.filtfilt(b, a, series)


def remove_baseline(ppg, fs, cutoff=None):
    """去基线：高通滤波(默认 0.5 Hz)。"""
    cutoff = cutoff or cfg.FILTER["base_line_hp"]
    return _filt(ppg, fs, "highpass", high=cutoff, order=cfg.FILTER["order"])


def bandpass(ppg, fs, low=None, high=None):
    """带通滤波(默认 0.5–8 Hz)。"""
    low = low or cfg.FILTER["band_low"]
    high = high or cfg.FILTER["band_high"]
    return _filt(ppg, fs, "bandpass", low=low, high=high, order=cfg.FILTER["order"])


def outlier_check(ppg, fs, params=None):
    """
    异常点检查：只抓会干扰脉搏检测的显著尖峰/离群点，避免误伤正常脉搏波。
      1) 差分域：|x[n]-x[n-1]| 超过 max(k*MAD, 分位数下限) 判为跳变尖峰；
      2) 长窗幅度域：样本偏离长窗局部中值超过 k*MAD(长窗) 判为离群(饱和/爆点)。
    异常点用邻域线性插值修复。返回 (clean, mask, stats)。
    """
    p = params or {}
    k = p.get("mad_k", cfg.OUTLIER["mad_k"])
    floor_pct = p.get("diff_floor_pct", cfg.OUTLIER["diff_floor_pct"])
    amp_k = p.get("amp_k", cfg.OUTLIER["amp_k"])
    amp_win = max(5, int(p.get("amp_win_sec", cfg.OUTLIER["amp_win_sec"]) * fs))
    x = ppg.astype(np.float64).copy()

    # ---- 1) 差分域：跳变尖峰 ----
    d = np.abs(np.diff(x, prepend=x[0]))
    med_d = np.median(d)
    mad_d = np.median(np.abs(d - med_d))
    floor = np.percentile(d, floor_pct)
    thresh_d = max(med_d + k * (mad_d + 1e-12), floor)
    outlier_diff = d > thresh_d

    # ---- 2) 长窗幅度域：绝对离群/饱和 ----
    if amp_win % 2 == 0:
        amp_win += 1
    base = signal.medfilt(x, kernel_size=amp_win)
    resid = x - base
    med_r = np.median(resid)
    mad_r = np.median(np.abs(resid - med_r))
    thresh_r = med_r + amp_k * (mad_r + 1e-12)
    outlier_amp = np.abs(resid - med_r) > thresh_r

    mask = outlier_diff | outlier_amp
    # 修复：异常点用相邻正常值线性插值
    clean = x.copy()
    idx = np.where(~mask)[0]
    if idx.size > 0 and mask.any():
        clean[mask] = np.interp(np.where(mask)[0], idx, x[idx])
    stats = dict(
        n_total=len(x),
        n_outlier=int(mask.sum()),
        outlier_ratio=float(mask.sum() / len(x)),
        mad_diff=float(mad_d),
        thresh_diff=float(thresh_d),
        mad_amp=float(mad_r),
        thresh_amp=float(thresh_r),
    )
    return clean, mask, stats


def preprocess_channel(ppg, ax, ay, az, fs_ppg, fs_acc, params=None):
    """完整单通道预处理管线。返回结果 dict。"""
    # 1) 运动伪影去除
    no_artifact, motion_mask, motion_int, w = motion_artifact_removal(
        ppg, ax, ay, az, fs_ppg, fs_acc, params)
    # 2) 去基线
    no_base = remove_baseline(no_artifact, fs_ppg)
    # 3) 带通滤波
    filtered = bandpass(no_base, fs_ppg)
    # 4) 异常点检查
    clean, outlier_mask, out_stats = outlier_check(filtered, fs_ppg, params)
    return dict(
        raw=ppg,
        no_artifact=no_artifact,
        filtered=filtered,
        clean=clean,
        motion_mask=motion_mask,
        motion_intensity=motion_int,
        outlier_mask=outlier_mask,
        outlier_stats=out_stats,
        ref_weights=w,
    )
