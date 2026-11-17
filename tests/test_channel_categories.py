
"""频道分类统一与订阅排序专项测试。"""

import tempfile
import unittest
from pathlib import Path

import publisher
from normalizer import load_channels, normalize_source_content


class ChannelCategoryTests(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.channels_path = Path(self.temp_dir.name) / "channels.txt"
        self.channels_path.write_text(
            "# 中央频道\n"
            "CCTV-1\n"
            "CCTV-6\n"
            "\n"
            "# 卫视频道\n"
            "湖南卫视\n"
            "广东卫视\n"
            "\n"
            "# 港澳台频道\n"
            "翡翠台\n"
            "\n"
            "# 体育频道\n"
            "CCTV-5\n"
            "CCTV-5+\n"
            "Eurosport 1\n",
            encoding="utf-8",
        )
        self.channels = load_channels(self.channels_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _normalize(self, channel, url, source_group, order):
        content = (
            "#EXTM3U\n"
            f'#EXTINF:-1 group-title="{source_group}" tvg-name="{channel}",{channel}\n'
            f"{url}\n"
        )
        result = normalize_source_content(
            content=content,
            source={
                "url": "https://example.com/source.m3u",
                "final_url": "https://example.com/source.m3u",
                "source_type": "automatic",
                "priority": 1,
                "order": order,
            },
            channels=self.channels,
            max_candidates_per_source=20,
            seen_urls=set(),
            content_type="application/vnd.apple.mpegurl",
        )
        self.assertEqual(len(result["candidates"]), 1)
        candidate = result["candidates"][0]
        candidate["selection"] = {"status": publisher.ACCEPTED}
        return candidate

    def test_channel_config_keeps_list_compatibility_and_categories(self):
        self.assertIsInstance(self.channels, list)
        self.assertEqual(
            self.channels.category_by_channel["CCTV-6"],
            "央视频道",
        )
        self.assertEqual(
            self.channels.category_by_channel["湖南卫视"],
            "卫视频道",
        )
        self.assertEqual(
            self.channels.category_by_channel["翡翠台"],
            "港澳台频道",
        )
        self.assertEqual(
            self.channels.category_by_channel["CCTV-5"],
            "体育频道",
        )

    def test_source_group_is_ignored_and_routes_are_sorted(self):
        candidates = [
            self._normalize(
                "Eurosport 1",
                "https://example.com/eurosport.m3u8",
                "Sports",
                0,
            ),
            self._normalize(
                "湖南卫视",
                "https://example.com/hunan.m3u8",
                "综合",
                1,
            ),
            self._normalize(
                "CCTV-6",
                "https://example.com/cctv6.m3u8",
                "央视台",
                2,
            ),
            self._normalize(
                "CCTV-5",
                "https://example.com/cctv5.m3u8",
                "央视频道",
                3,
            ),
            self._normalize(
                "CCTV-5",
                "https://example.com/cctv5-backup.m3u8",
                "综合",
                4,
            ),
        ]

        output = publisher.build_subscriptions(
            candidates,
            self.channels,
        )

        entries = []
        lines = output["m3u"].splitlines()

        for index, line in enumerate(lines):
            if line.startswith("#EXTINF:"):
                group = line.split('group-title="', 1)[1].split('"', 1)[0]
                channel = line.split(",", 1)[1]
                entries.append((group, channel, lines[index + 1]))

        self.assertEqual(
            [(group, channel) for group, channel, _ in entries],
            [
                ("央视频道", "CCTV-6"),
                ("卫视频道", "湖南卫视"),
                ("体育频道", "CCTV-5"),
                ("体育频道", "CCTV-5"),
                ("体育频道", "Eurosport 1"),
            ],
        )

        self.assertNotIn('group-title="综合"', output["m3u"])
        self.assertNotIn('group-title="央视台"', output["m3u"])
        self.assertNotIn('group-title="Sports"', output["m3u"])

        # 原有线路全部保留，TXT 与 M3U 的顺序一致。
        self.assertEqual(output["route_count"], 5)
        self.assertEqual(
            [line.split(",", 1)[0] for line in output["txt"].splitlines()],
            [channel for _, channel, _ in entries],
        )

        # CCTV-5 的两条线路仍然相邻。
        self.assertEqual(
            [channel for _, channel, _ in entries].count("CCTV-5"),
            2,
        )
        self.assertEqual(
            [channel for _, channel, _ in entries].index("CCTV-5"),
            2,
        )

    def test_unclassified_channel_is_rejected_early(self):
        self.channels_path.write_text(
            "# 中央频道\nCCTV-1\n未分类频道\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            ValueError,
            "未归入四大分类",
        ):
            load_channels(self.channels_path)


if __name__ == "__main__":
    unittest.main()
