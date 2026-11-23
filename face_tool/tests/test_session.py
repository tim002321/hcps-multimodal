# -*- coding: utf-8 -*-
"""会话发现与数据库兼容。用本机真实会话做只读校验，找不到就跳过。"""
import os
import unittest

from facetool import db as db_mod
from facetool.session import discover_session

# 样本会话路径从环境变量读取，仓库里不放真实被试姓名。
# 本地跑集成测试前先设置：
#   set HCP_SAMPLE_SESSION=F:\...\data\<被试>\<会话目录>
NEW_SESSION = os.environ.get('HCP_SAMPLE_SESSION', '')
LEGACY_DB = (r'F:\hcpsproject\PyCharmMiscProject\AAAwindows_code\data\123'
             r'\123_20261103_142125\20261103_142134.db')


class TestNewSession(unittest.TestCase):
    @unittest.skipUnless(os.path.isdir(NEW_SESSION), '样本会话不存在')
    def test_discover(self):
        info = discover_session(NEW_SESSION)
        self.assertEqual(info.problem, '')
        self.assertEqual(info.total, 345)
        self.assertEqual(len(info.prefix_counts), 1)
        self.assertEqual(info.width, 640)
        self.assertEqual(info.height, 480)
        self.assertGreater(info.duration_s, 20)
        self.assertEqual(info.db_kind, 'new')

    @unittest.skipUnless(os.path.isdir(NEW_SESSION), '样本会话不存在')
    def test_names_match_disk(self):
        info = discover_session(NEW_SESSION)
        names = {f.name for f in info.frames}
        on_disk = set(os.listdir(info.frames_dir))
        self.assertTrue(names.issubset(on_disk))

    @unittest.skipUnless(os.path.isdir(NEW_SESSION), '样本会话不存在')
    def test_frames_sorted_by_id(self):
        info = discover_session(NEW_SESSION)
        seqs = [f.frame_index for f in info.frames]
        self.assertEqual(seqs, sorted(seqs))


class TestLegacyDb(unittest.TestCase):
    @unittest.skipUnless(os.path.exists(LEGACY_DB), '老库样本不存在')
    def test_probe_legacy(self):
        probe = db_mod.probe_db(LEGACY_DB)
        self.assertTrue(probe.has_table)
        self.assertTrue(probe.legacy)
        self.assertIn('jpeg', probe.columns)
        self.assertGreater(probe.row_count, 1000)

    @unittest.skipUnless(os.path.exists(LEGACY_DB), '老库样本不存在')
    def test_synthetic_prefix(self):
        info = discover_session(os.path.dirname(LEGACY_DB))
        self.assertEqual(info.db_kind, 'legacy')
        prefixes = info.prefixes()
        self.assertEqual(len(prefixes), 1)
        self.assertEqual(prefixes[0], '20261103_142134_000000')


class TestMissing(unittest.TestCase):
    def test_missing_dir(self):
        info = discover_session(r'Z:\definitely\not\here')
        self.assertNotEqual(info.problem, '')

    def test_empty_dir(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            info = discover_session(tmp)
            self.assertNotEqual(info.problem, '')


if __name__ == '__main__':
    unittest.main()
