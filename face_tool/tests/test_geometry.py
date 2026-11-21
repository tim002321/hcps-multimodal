# -*- coding: utf-8 -*-
"""几何计算：EAR / MAR / ROI 裁剪。用构造点验证，不依赖模型。"""
import unittest

import numpy as np

from facetool import geometry
from facetool.detector import Face
from facetool.params import RunParams


def synthetic_face(ear=0.32, mar=0.10):
    """造一组满足 EAR / MAR 期望值的 68 点。"""
    pts = np.zeros((68, 2), dtype=np.float32)

    # 眼睛：水平宽 40，垂直张开由 ear 决定（EAR = 2*v / (2*w) = v/w）
    width = 40.0
    height = ear * width
    for base, cx in ((36, 100.0), (42, 180.0)):
        cy = 100.0
        pts[base + 0] = (cx - width / 2, cy)                 # 外眼角
        pts[base + 1] = (cx - width / 4, cy - height / 2)    # 上睑(外)
        pts[base + 2] = (cx + width / 4, cy - height / 2)    # 上睑(内)
        pts[base + 3] = (cx + width / 2, cy)                 # 内眼角
        pts[base + 4] = (cx + width / 4, cy + height / 2)    # 下睑(内)
        pts[base + 5] = (cx - width / 4, cy + height / 2)    # 下睑(外)

    # 嘴：外唇宽 60，内唇张开 = mar * 内唇宽(40)
    mw, iw = 60.0, 40.0
    cxm, cym = 140.0, 200.0
    pts[48] = (cxm - mw / 2, cym)
    pts[54] = (cxm + mw / 2, cym)
    pts[51] = (cxm, cym - 8)                    # 上外唇
    pts[57] = (cxm, cym + 8)                    # 下外唇
    pts[60] = (cxm - iw / 2, cym)
    pts[62] = (cxm, cym - mar * iw / 2)
    pts[64] = (cxm + iw / 2, cym)
    pts[66] = (cxm, cym + mar * iw / 2)

    # 其余点塞在合理位置，validate 只看眼/嘴序关系
    for i in range(68):
        if not pts[i].any():
            pts[i] = (140.0, 60.0)
    for i in range(0, 17):
        pts[i] = (60 + i * 10, 240)
    for i in range(17, 27):
        pts[i] = (60 + (i - 17) * 16, 70)
    for i in range(27, 36):
        pts[i] = (140, 80 + (i - 27) * 10)
    return pts


class TestMetrics(unittest.TestCase):
    def test_ear_matches_design(self):
        for want in (0.20, 0.32, 0.45):
            pts = synthetic_face(ear=want)
            got = geometry.ear(pts, geometry.LEFT_EYE)
            self.assertAlmostEqual(got, want, places=3)
            self.assertAlmostEqual(geometry.ear(pts, geometry.RIGHT_EYE), want, places=3)

    def test_mar_matches_design(self):
        for want in (0.0, 0.10, 0.60):
            pts = synthetic_face(mar=want)
            self.assertAlmostEqual(geometry.mar_inner(pts), want, places=3)

    def test_plausible_ranges(self):
        self.assertTrue(geometry.plausible_ear(0.30))
        self.assertFalse(geometry.plausible_ear(0.90))
        self.assertTrue(geometry.plausible_mar(0.0))
        self.assertFalse(geometry.plausible_mar(9.0))


class TestRoi(unittest.TestCase):
    def setUp(self):
        self.pts = synthetic_face()
        self.face = Face(60, 40, 220, 260, 0.9)
        self.params = RunParams()

    def test_eye_roi_contains_eye(self):
        rect = geometry.eye_roi(self.pts, 'L', self.face, self.params)
        self.assertLessEqual(rect.x1, self.pts[36][0])
        self.assertGreaterEqual(rect.x2, self.pts[39][0])
        self.assertGreater(rect.width, 0)
        self.assertGreater(rect.height, 0)

    def test_eye_roi_not_collapse_on_blink(self):
        closed = synthetic_face(ear=0.05)
        rect = geometry.eye_roi(closed, 'L', self.face, self.params)
        open_rect = geometry.eye_roi(self.pts, 'L', self.face, self.params)
        # 闭眼时高度不应比睁眼小太多（取 max 的作用）
        self.assertGreater(rect.height, open_rect.height * 0.8)

    def test_mouth_roi_centered_below(self):
        rect = geometry.mouth_roi(self.pts, self.params)
        self.assertGreater(rect.width, 60)
        self.assertGreater(rect.height, 16)

    def test_clip_inside_frame(self):
        rect = geometry.Rect(-50, -50, 9999, 9999)
        clipped = geometry.clip_rect(rect, (480, 640))
        self.assertEqual(clipped.as_tuple(), (0, 0, 640, 480))

    def test_crop_returns_patch(self):
        image = np.zeros((480, 640, 3), dtype=np.uint8)
        rect = geometry.eye_roi(self.pts, 'R', self.face, self.params)
        patch = geometry.crop(image, rect)
        self.assertEqual(patch.shape[:2], (rect.height, rect.width))

    def test_scale_roi(self):
        image = np.zeros((20, 30, 3), dtype=np.uint8)
        self.assertEqual(geometry.scale_roi(image, 2.0).shape[:2], (40, 60))
        self.assertEqual(geometry.scale_roi(image, 1.0).shape[:2], (20, 30))


class TestParams(unittest.TestCase):
    def test_hash_changes_with_roi(self):
        a = RunParams()
        b = RunParams(eye_w_ratio=a.eye_w_ratio + 0.06)
        self.assertNotEqual(a.params_hash(), b.params_hash())

    def test_hash_changes_with_eye_format(self):
        a = RunParams()
        b = RunParams(eye_format='jpg' if a.eye_format == 'png' else 'png')
        self.assertNotEqual(a.params_hash(), b.params_hash())

    def test_hash_ignores_runtime_flags(self):
        a = RunParams()
        b = RunParams(limit=10, overwrite=True, annotate=False, device='cpu')
        self.assertEqual(a.params_hash(), b.params_hash())

    def test_problems_detects_bad_values(self):
        self.assertTrue(RunParams(eye_w_ratio=3.0).problems())


if __name__ == '__main__':
    unittest.main()
