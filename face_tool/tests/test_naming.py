# -*- coding: utf-8 -*-
"""帧名解析与合成。用 unittest，不依赖 pytest。"""
import os
import unittest

from facetool import naming


class TestParse(unittest.TestCase):
    def test_standard_name(self):
        parsed = naming.parse_stem('20261117_192821_202189_000001')
        self.assertEqual(parsed, ('20261117_192821_202189', 1))

    def test_parse_full_name(self):
        got = naming.parse_name('20261117_192821_202189_000345.jpg')
        self.assertEqual(got, ('20261117_192821_202189', 345, '.jpg'))

    def test_reject_nonstandard(self):
        for bad in ('frame_000001', '20261117_192821_000001', 'abc',
                    '20261117_192821_202189_01'):
            self.assertIsNone(naming.parse_stem(bad), bad)

    def test_round_trip_keeps_33_chars(self):
        prefix = '20261117_192821_202189'
        name = naming.render_name(prefix, 1)
        self.assertEqual(name, '20261117_192821_202189_000001.jpg')
        self.assertEqual(len(name), 33)
        self.assertEqual(naming.parse_name(name), (prefix, 1, '.jpg'))

    def test_prefix_datetime(self):
        moment = naming.prefix_to_datetime('20261117_192821_202189')
        self.assertEqual(moment.strftime('%Y-%m-%d %H:%M:%S.%f'),
                         '2026-11-17 19:28:21.202189')


class TestSynthetic(unittest.TestCase):
    def test_from_db_stem(self):
        self.assertEqual(naming.synthetic_prefix('20261103_142134'),
                         '20261103_142134_000000')
        self.assertEqual(len(naming.synthetic_prefix('20261103_142134')), 22)

    def test_unnamed_db(self):
        got = naming.synthetic_prefix('abc')
        self.assertEqual(len(got), 22)


class TestSafe(unittest.TestCase):
    def test_strip_illegal(self):
        # 非法字符替换成下划线（Windows 下 ':' 会被 ntpath 当盘符，这里用其他字符）
        self.assertEqual(naming.safe_filename('x<y>z.jpg'), 'x_y_z.jpg')
        self.assertEqual(naming.safe_filename('a|b?.jpg'), 'a_b_.jpg')

    def test_strip_path_part(self):
        # 含路径分隔符时只保留最后一段（basename 语义）
        self.assertEqual(naming.safe_filename(r'dir\a:b.jpg'), 'a_b.jpg')

    def test_keep_standard(self):
        name = '20261117_192821_202189_000001.jpg'
        self.assertEqual(naming.safe_filename(name), name)

    def test_unknown_ext(self):
        self.assertTrue(naming.safe_filename('x.txt').endswith('.jpg'))


if __name__ == '__main__':
    unittest.main()
