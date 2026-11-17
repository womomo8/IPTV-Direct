"""频道分类配置动态读取、来源分类隔离及订阅输出测试。"""

import tempfile
import unittest
from pathlib import Path

import publisher
from normalizer import (
    ChannelConfigError,
    load_channels,
    normalize_source_content,
)


CATEGORY_TEXT = """\
# 新闻综合频道
新闻一
新闻二

# 空分类

# 体育赛事
篮球赛事

# 海外频道
ESPN
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

    def test_category_names_and_order_are_read_from_file(self):
        self.assertEqual(
            self.channels.category_order,
            ("新闻综合频道", "空分类", "体育赛事", "海外频道"),
        )

    def test_channel_category_mapping(self):
        self.assertEqual(
            self.channels.category_by_channel,
            {
                "新闻一": "新闻综合频道",
                "新闻二": "新闻综合频道",
                "篮球赛事": "体育赛事",
                "ESPN": "海外频道",
            },
        )

    def test_empty_category_is_allowed(self):
        self.assertIn("空分类", self.channels.category_order)
        self.assertFalse(
            any(
                category == "空分类"
                for category in self.channels.category_by_channel.values()
            )
        )

    def test_category_count_names_and_order_can_change(self):
        content = """\
# 新分类
频道甲

# 第二分类
频道乙

# 新增空分类
"""
        self.write_config(content)
        channels = load_channels(self.path)

        self.assertEqual(
            channels.category_order,
            ("新分类", "第二分类", "新增空分类"),
        )
        self.assertEqual(
            channels.category_by_channel,
            {
                "频道甲": "新分类",
                "频道乙": "第二分类",
            },
        )

    def test_duplicate_category_is_rejected(self):
        content = CATEGORY_TEXT + "\n# 新闻综合频道\n"
        self.write_config(content)

        with self.assertRaisesRegex(
            ChannelConfigError,
            "分类标题重复",
        ):
            load_channels(self.path)

    def test_case_insensitive_duplicate_category_is_rejected(self):
        self.write_config(
            "# Sports\n频道甲\n# SPORTS\n频道乙\n"
        )

        with self.assertRaisesRegex(
            ChannelConfigError,
            "分类标题重复",
        ):
            load_channels(self.path)

    def test_empty_category_heading_is_rejected(self):
        self.write_config("# 新闻综合频道\n频道甲\n#   \n")

        with self.assertRaisesRegex(
            ChannelConfigError,
            "空分类标题",
        ):
            load_channels(self.path)

    def test_duplicate_channel_assignment_is_rejected(self):
        self.write_config(
            "# 新闻综合频道\n频道甲\n"
            "# 体育赛事\n频道甲\n"
        )

        with self.assertRaisesRegex(
            ChannelConfigError,
            "频道重复配置",
        ):
            load_channels(self.path)

    def test_channel_before_first_category_is_rejected(self):
        self.write_config("频道甲\n# 新闻综合频道\n频道乙\n")

        with self.assertRaises(ChannelConfigError):
            load_channels(self.path)

    def test_missing_category_heading_is_rejected(self):
        self.write_config("频道甲\n频道乙\n")

        with self.assertRaises(ChannelConfigError):
            load_channels(self.path)

    def test_empty_channel_list_is_rejected(self):
        self.write_config("# 空分类一\n# 空分类二\n")

        with self.assertRaisesRegex(
            ChannelConfigError,
            "没有配置目标频道",
        ):
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
            '#EXTINF:-1 group-title="海外频道",新闻一\n'
            "https://example.com/news.m3u8\n"
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
        self.assertEqual(
            result["candidates"][0]["category"],
            "新闻综合频道",
        )

    @staticmethod
    def make_route(channel, url, category, order):
        return {
            "channel": channel,
            "normalized_url": url,
            "category": category,
            "priority": 1,
            "source_order": order,
            "selection": {
                "status": publisher.ACCEPTED,
            },
        }

    def test_publisher_uses_configured_category_and_order(self):
        routes = [
            self.make_route(
                "ESPN",
                "https://example.com/espn.m3u8",
                "海外频道",
                0,
            ),
            self.make_route(
                "篮球赛事",
                "https://example.com/basketball.m3u8",
                "体育赛事",
                1,
            ),
            self.make_route(
                "新闻二",
                "https://example.com/news2.m3u8",
                "新闻综合频道",
                2,
            ),
        ]

        output = publisher.build_subscriptions(
            routes,
            self.channels,
        )

        entries = []
        lines = output["m3u"].splitlines()

        for index, line in enumerate(lines):
            if line.startswith("#EXTINF:"):
                group = (
                    line.split('group-title="', 1)[1]
                    .split('"', 1)[0]
                )
                channel = line.split(",", 1)[1]
                entries.append((group, channel, lines[index + 1]))

        self.assertEqual(
            [(group, channel) for group, channel, _ in entries],
            [
                ("新闻综合频道", "新闻二"),
                ("体育赛事", "篮球赛事"),
                ("海外频道", "ESPN"),
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
            '#EXTINF:-1 group-title="体育赛事",新闻一\n'
            "https://example.com/news.m3u8\n"
        )
        txt = "新闻一,https://example.com/news.m3u8\n"

        with self.assertRaises(publisher.PublisherError):
            publisher.validate_subscriptions(
                m3u,
                txt,
                self.channels,
            )

    def test_m3u_and_txt_channel_must_match(self):
        m3u = (
            "#EXTM3U\n"
            '#EXTINF:-1 group-title="新闻综合频道",新闻一\n'
            "https://example.com/news.m3u8\n"
        )
        txt = "新闻二,https://example.com/news.m3u8\n"

        with self.assertRaises(publisher.PublisherError):
            publisher.validate_subscriptions(
                m3u,
                txt,
                self.channels,
            )


if __name__ == "__main__":
    unittest.main()
