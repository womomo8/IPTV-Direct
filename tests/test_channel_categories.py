
"""六大频道分类、来源分类隔离及订阅输出专项测试。"""

import tempfile
import unittest
from pathlib import Path

import publisher
from normalizer import CATEGORY_ORDER, ChannelConfigError, load_channels, normalize_source_content


CATEGORY_TEXT = """\
# 央视频道
CCTV-1
CCTV-5
CCTV-5+
CCTV-6

# 卫视频道
湖南卫视
广东卫视

# 港澳台
翡翠台

# 体育频道
广东体育
五星体育

# 国外频道
NBA TV
ESPN
Eurosport 1

# 其他频道
地方综合频道
"""


class ChannelCategoryTests(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "channels.txt"
        self.path.write_text(CATEGORY_TEXT, encoding="utf-8")
        self.channels = load_channels(self.path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def write_config(self, content):
        self.path.write_text(content, encoding="utf-8")

    def test_six_categories_and_order(self):
        self.assertEqual(
            CATEGORY_ORDER,
            ("央视频道", "卫视频道", "港澳台", "体育频道", "国外频道", "其他频道"),
        )

    def test_all_cctv_channels_are_central(self):
        for channel in ("CCTV-1", "CCTV-5", "CCTV-5+", "CCTV-6"):
            self.assertEqual(
                self.channels.category_by_channel[channel],
                "央视频道",
            )

    def test_satellite_channels(self):
        for channel in ("湖南卫视", "广东卫视"):
            self.assertEqual(
                self.channels.category_by_channel[channel],
                "卫视频道",
            )

    def test_hong_kong_macao_taiwan(self):
        self.assertEqual(
            self.channels.category_by_channel["翡翠台"],
            "港澳台",
        )

    def test_domestic_sports(self):
        for channel in ("广东体育", "五星体育"):
            self.assertEqual(
                self.channels.category_by_channel[channel],
                "体育频道",
            )

    def test_foreign_sports_belong_to_foreign_category(self):
        for channel in ("NBA TV", "ESPN", "Eurosport 1"):
            self.assertEqual(
                self.channels.category_by_channel[channel],
                "国外频道",
            )

    def test_empty_other_category_is_allowed(self):
        self.write_config(CATEGORY_TEXT.replace("地方综合频道\n", ""))
        channels = load_channels(self.path)
        self.assertNotIn("地方综合频道", channels)

    def test_missing_category_is_rejected(self):
        self.write_config(CATEGORY_TEXT.replace("# 港澳台\n翡翠台\n\n", ""))
        with self.assertRaises(ChannelConfigError):
            load_channels(self.path)

    def test_duplicate_category_is_rejected(self):
        self.write_config(
            CATEGORY_TEXT.replace(
                "# 国外频道\n",
                "# 国外频道\n# 国外频道\n",
            )
        )
        with self.assertRaisesRegex(ChannelConfigError, "分类标题重复"):
            load_channels(self.path)

    def test_unknown_category_is_rejected(self):
        self.write_config(
            CATEGORY_TEXT.replace("# 港澳台\n", "# 港澳台频道\n")
        )
        with self.assertRaisesRegex(ChannelConfigError, "未知分类标题"):
            load_channels(self.path)

    def test_wrong_category_order_is_rejected(self):
        content = CATEGORY_TEXT.replace(
            "# 港澳台\n翡翠台\n\n# 体育频道\n广东体育\n五星体育",
            "# 体育频道\n广东体育\n五星体育\n\n# 港澳台\n翡翠台",
        )
        self.write_config(content)
        with self.assertRaises(ChannelConfigError):
            load_channels(self.path)

    def test_duplicate_channel_assignment_is_rejected(self):
        self.write_config(
            CATEGORY_TEXT.replace(
                "# 卫视频道\n",
                "# 卫视频道\nCCTV-5\n",
            )
        )
        with self.assertRaisesRegex(ChannelConfigError, "频道重复配置"):
            load_channels(self.path)

    def test_channel_before_first_category_is_rejected(self):
        self.write_config("CCTV-1\n" + CATEGORY_TEXT)
        with self.assertRaises(ChannelConfigError):
            load_channels(self.path)

    def test_source_group_title_cannot_override_config(self):
        source = {
            "url": "https://example.com/source.m3u",
            "final_url": "https://example.com/source.m3u",
            "source_type": "automatic",
            "priority": 1,
            "order": 0,
        }
        content = (
            "#EXTM3U\n"
            '#EXTINF:-1 group-title="国外体育",CCTV-5\n'
            "https://example.com/cctv5.m3u8\n"
        )
        result = normalize_source_content(
            content,
            source,
            self.channels,
            20,
            set(),
            content_type="application/vnd.apple.mpegurl",
        )
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(result["candidates"][0]["category"], "央视频道")

    @staticmethod
    def make_route(channel, url, category, order):
        return {
            "channel": channel,
            "normalized_url": url,
            "category": category,
            "priority": 1,
            "source_order": order,
            "selection": {"status": publisher.ACCEPTED},
        }

    def test_publisher_uses_configured_category_and_order(self):
        routes = [
            self.make_route(
                "Eurosport 1",
                "https://example.com/eurosport.m3u8",
                "国外频道",
                0,
            ),
            self.make_route(
                "广东体育",
                "https://example.com/sports.m3u8",
                "体育频道",
                1,
            ),
            self.make_route(
                "CCTV-6",
                "https://example.com/cctv6.m3u8",
                "央视频道",
                2,
            ),
            self.make_route(
                "翡翠台",
                "https://example.com/jade.m3u8",
                "港澳台",
                3,
            ),
            self.make_route(
                "湖南卫视",
                "https://example.com/hunan.m3u8",
                "卫视频道",
                4,
            ),
        ]

        output = publisher.build_subscriptions(routes, self.channels)

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
                ("港澳台", "翡翠台"),
                ("体育频道", "广东体育"),
                ("国外频道", "Eurosport 1"),
            ],
        )

        self.assertTrue(
            publisher.validate_subscriptions(
                output["m3u"],
                output["txt"],
                self.channels,
            )
        )

    def test_wrong_m3u_group_title_is_rejected(self):
        m3u = (
            "#EXTM3U\n"
            '#EXTINF:-1 group-title="体育频道",CCTV-5\n'
            "https://example.com/cctv5.m3u8\n"
        )
        txt = "CCTV-5,https://example.com/cctv5.m3u8\n"

        with self.assertRaises(publisher.PublisherError):
            publisher.validate_subscriptions(m3u, txt, self.channels)

    def test_m3u_and_txt_order_must_match(self):
        m3u = (
            "#EXTM3U\n"
            '#EXTINF:-1 group-title="央视频道",CCTV-5\n'
            "https://example.com/cctv5.m3u8\n"
        )
        txt = "CCTV-6,https://example.com/cctv5.m3u8\n"

        with self.assertRaises(publisher.PublisherError):
            publisher.validate_subscriptions(m3u, txt, self.channels)


if __name__ == "__main__":
    unittest.main()
