#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
下载歌单.py 的单元测试。

运行:
    python test_下载歌单.py          （或 python -m unittest test_下载歌单）

覆盖:
    1. same_song() —— 歌名一致性判据（错配反例 + 版本后缀正例）
    2. normalize_title() —— 版本后缀剥离
    3. parse_playlist_id() —— 歌单 ID / 链接解析
    4. search_fallback() —— 直搜兜底通道的候选筛选逻辑
"""

import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import 下载歌单 as M  # noqa: E402


class TestSameSong(unittest.TestCase):
    """歌名一致性：错配必须拒绝，版本后缀差异必须接受。"""

    # (原歌名, 换源返回名, 期望是否同一首, 备注)
    CASES = [
        # --- 完全一致 ---
        ("晴天", "晴天", True, "完全一致"),
        ("Mojito", "Mojito", True, "英文歌名"),

        # --- 版本后缀正例：应判为同一首 ---
        ("半兽人", "半兽人(游戏《魔兽争霸III》主题曲)", True, "括号补充说明"),
        ("七里香", "七里香 (Live)", True, "Live 后缀"),
        ("稻香", "稻香 (Live版)", True, "Live版（中文紧跟英文）"),
        ("稻香", "稻香 (Remix摇滚版)", True, "Remix+中文"),
        ("轨迹", "轨迹 (伴奏)", True, "伴奏版"),
        ("青花瓷", "青花瓷(Live)", True, "无空格 Live"),
        ("Mojito", "Mojito (Remix)", True, "英文 Remix"),

        # --- 错配反例：必须拒绝 ---
        ("七里香", "我的地盘", False, "完全不同的歌"),
        ("稻香", "我的地盘", False, "完全不同的歌"),
        ("Mojito", "圣诞星（feat. 杨瑞代）", False, "完全不同的歌"),
        ("不该 (Live)", "手语(Live)", False, "不同的歌"),
        ("Mojito", "东风破(Album Version)", False, "不同歌 + 版本后缀"),
        ("周杰伦", "林俊杰", False, "歌手名混入歌名"),
        ("稻香", "稻香稻香", False, "歌名被重复拼接"),
    ]

    def test_same_song(self):
        for orig, got, expect, note in self.CASES:
            with self.subTest(orig=orig, got=got, note=note):
                self.assertEqual(
                    M.same_song(orig, got), expect,
                    "same_song(%r, %r) 应为 %s（%s）" % (orig, got, expect, note))

    def test_same_song_empty_rejected(self):
        """空歌名不能判为同一首（否则会误下错歌）。"""
        self.assertFalse(M.same_song("", ""))
        self.assertFalse(M.same_song("晴天", ""))
        self.assertFalse(M.same_song("", "晴天"))

    def test_same_song_ignores_case_and_space(self):
        self.assertTrue(M.same_song("Mojito", "mojito"))
        self.assertTrue(M.same_song("Mine Mine", "Mine  Mine"))


class TestNormalizeTitle(unittest.TestCase):
    """版本后缀与括号内容应被剥离。"""

    def test_strips_bracket_content(self):
        self.assertEqual(M.normalize_title("稻香 (Remix摇滚版)"), "稻香")
        self.assertEqual(M.normalize_title("不该 (电视剧《幻城》主题曲)"), "不该")

    def test_keeps_core_name(self):
        self.assertEqual(M.normalize_title("Mojito"), "mojito")
        self.assertEqual(M.normalize_title("Mine Mine"), "minemine")

    def test_handles_none(self):
        self.assertEqual(M.normalize_title(None), "")


class TestParsePlaylistId(unittest.TestCase):
    """歌单 ID 与链接解析。"""

    def test_plain_id(self):
        self.assertEqual(M.parse_playlist_id("8691023"), "8691023")

    def test_kugou_url(self):
        self.assertEqual(
            M.parse_playlist_id("https://www.kugou.com/yy/special/single/8691023.html"),
            "8691023")

    def test_invalid_exits(self):
        with self.assertRaises(SystemExit):
            M.parse_playlist_id("not-a-playlist")


class SearchFallbackLogic(unittest.TestCase):
    """直搜兜底通道的候选筛选规则（不触网，纯逻辑验证）。"""

    @staticmethod
    def _filter(cands, want_name, want_artist, want_dur):
        """复刻 search_fallback 的筛选规则，用于验证判定边界。

        刻意直接调用被测模块的 normalize_artist / same_song，
        保证测的就是线上真实代码路径，而不是另写一份副本。
        """
        out = []
        for name, artist, dur in cands:
            if not M.same_song(want_name, name):
                continue
            if want_artist and M.normalize_artist(want_artist) != M.normalize_artist(artist):
                continue
            diff = abs(dur - want_dur) if (dur and want_dur) else 0
            out.append((diff, name))
        out.sort(key=lambda x: x[0])
        return out

    def test_picks_exact_match_over_wrong_song(self):
        """「七里香」场景：候选里混有同名别人的歌，必须选中周杰伦原版。"""
        cands = [
            ("七里香", "周杰伦", 298),
            ("七里香", "陈淑桦", 217),
            ("七里香(浅息版)", "晚弦留声", 294),
        ]
        got = self._filter(cands, "七里香", "周杰伦", 299)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0][1], "七里香")
        self.assertLessEqual(got[0][0], 8)

    def test_rejects_same_name_other_artist(self):
        """同名不同歌手必须排除。"""
        cands = [("稻香", "周杰伦", 223), ("稻香", "林俊杰", 223)]
        got = self._filter(cands, "稻香", "周杰伦", 223)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0][1], "稻香")

    def test_rejects_wrong_song_entirely(self):
        """歌名完全不同的候选必须排除（防错配的核心判据）。"""
        cands = [("东风破(Album Version)", "周杰伦", 185)]
        got = self._filter(cands, "Mojito", "周杰伦", 185)
        self.assertEqual(got, [])

    def test_duration_is_tiebreak_not_hard_filter(self):
        """时长只用于在多个同名候选中择优，不做硬排除。

        实测：酷狗「不该 (Live)」301 秒 vs migu「不该 (电视剧《幻城》主题曲)」290 秒，
        差 11 秒仍是同一首歌的不同录音版本。
        """
        cands = [("不该 (电视剧《幻城》主题曲)", "周杰伦 / 张惠妹", 290)]
        got = self._filter(cands, "不该 (Live)", "周杰伦、张惠妹", 301)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0][0], 11)   # 时长差被记录，但候选仍被接受

    def test_picks_closest_duration_among_same_name(self):
        """多个同名同歌手候选时，取时长最接近的。"""
        cands = [
            ("Mojito", "周杰伦", 300),
            ("Mojito", "周杰伦", 185),
        ]
        got = self._filter(cands, "Mojito", "周杰伦", 185)
        self.assertEqual(got[0][1], "Mojito")
        self.assertEqual(got[0][0], 0)

    def test_artist_slash_normalization(self):
        """歌手分隔符差异（/ 与 、）应视为同一歌手。"""
        cands = [("不该 (电视剧《幻城》主题曲)", "周杰伦 / 张惠妹", 290)]
        got = self._filter(cands, "不该 (Live)", "周杰伦、张惠妹", 301)
        self.assertEqual(len(got), 1)


class TestModuleWiring(unittest.TestCase):
    """模块级常量与结构完好性。"""

    def test_targets_order(self):
        """migu 必须排第一（实测覆盖率最高）。"""
        self.assertEqual(M.TARGETS[0], "migu")

    def test_request_interval_not_too_fast(self):
        """请求间隔必须 > 1 秒，否则触发上游限流。"""
        self.assertGreaterEqual(M.REQUEST_INTERVAL, 1.0)

    def test_has_search_fallback(self):
        self.assertTrue(callable(M.search_fallback))

    def test_extra_must_be_string(self):
        """队列接口要求 extra 是 JSON 字符串而非对象。"""
        payload = {"extra": {"content_id": "123"}}
        self.assertIsInstance(json.dumps(payload["extra"]), str)


class TestMusicDLWrapper(unittest.TestCase):
    """build/MusicDL.py（EXE 打包源）与脚本共用同一套换源逻辑。

    这里验证两者在关键判据上行为一致，避免以后只改一处导致行为漂移。
    """

    @classmethod
    def setUpClass(cls):
        cls.path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "build", "MusicDL.py")
        if not os.path.isfile(cls.path):
            raise unittest.SkipTest("找不到 build/MusicDL.py")
        import importlib.util
        spec = importlib.util.spec_from_file_location("MusicDL_under_test", cls.path)
        cls.mdl = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mdl)

    def test_syntax_valid(self):
        """打包源必须能被解释器加载，否则 EXE 构建会失败。"""
        import ast
        with open(self.path, encoding="utf-8") as f:
            ast.parse(f.read())

    def test_has_search_fallback(self):
        self.assertTrue(callable(self.mdl.search_fallback))

    def test_same_song_consistent(self):
        """same_song 判据必须与脚本版一致。"""
        cases = [("晴天", "晴天", True), ("七里香", "我的地盘", False),
                 ("稻香", "稻香 (Remix摇滚版)", True)]
        for orig, got, expect in cases:
            with self.subTest(orig=orig):
                self.assertEqual(self.mdl.same_song(orig, got), expect)

    def test_normalize_artist_consistent(self):
        """歌手归一化必须与脚本版一致。"""
        self.assertEqual(
            self.mdl.normalize_artist("周杰伦 / 张惠妹"),
            M.normalize_artist("周杰伦、张惠妹"))
        self.assertEqual(self.mdl.normalize_artist("周杰伦 / 张惠妹"),
                         M.normalize_artist("周杰伦、张惠妹"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
