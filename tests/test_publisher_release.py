
"""IPTV-Direct：正式订阅安全发布与回滚专项测试。"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import publisher


CHANNELS = ["CCTV-1", "CCTV-5", "CCTV-13", "CCTV-新闻"]


def make_subscriptions(entries):
    """根据频道和 URL 列表构造一致的 M3U/TXT。"""
    m3u_lines = ["#EXTM3U"]
    txt_lines = []

    for channel, url in entries:
        m3u_lines.append(f"#EXTINF:-1,{channel}")
        m3u_lines.append(url)
        txt_lines.append(f"{channel},{url}")

    return {
        "m3u": "\n".join(m3u_lines) + "\n",
        "txt": "\n".join(txt_lines) + "\n",
        "route_count": len(entries),
    }


def four_routes_two_channels():
    return make_subscriptions([
        ("CCTV-1", "https://example.com/cctv1-a.m3u8"),
        ("CCTV-1", "https://example.com/cctv1-b.m3u8"),
        ("CCTV-5", "https://example.com/cctv5-a.m3u8"),
        ("CCTV-5", "https://example.com/cctv5-b.m3u8"),
    ])


class PublisherReleaseTests(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.output_dir = Path(self.temp_dir.name) / "output"

    def tearDown(self):
        self.temp_dir.cleanup()

    def publish(self, subscriptions):
        return publisher.publish_subscriptions(
            subscriptions,
            CHANNELS,
            output_dir=self.output_dir,
        )

    def test_first_publish_creates_both_files(self):
        """首次发布应同时生成 M3U 和 TXT。"""
        subscriptions = make_subscriptions([
            ("CCTV-1", "https://example.com/cctv1.m3u8"),
            ("CCTV-5", "https://example.com/cctv5.m3u8"),
        ])

        result = self.publish(subscriptions)

        m3u_path = self.output_dir / "iptv.m3u"
        txt_path = self.output_dir / "iptv.txt"

        self.assertTrue(result["published"])
        self.assertTrue(result["first_publish"])
        self.assertTrue(m3u_path.is_file())
        self.assertTrue(txt_path.is_file())
        self.assertEqual(
            m3u_path.read_text(encoding="utf-8"),
            subscriptions["m3u"],
        )
        self.assertEqual(
            txt_path.read_text(encoding="utf-8"),
            subscriptions["txt"],
        )

    def test_existing_subscription_can_be_updated(self):
        """已有有效订阅应允许正常更新。"""
        old = make_subscriptions([
            ("CCTV-1", "https://example.com/old1.m3u8"),
            ("CCTV-5", "https://example.com/old5.m3u8"),
        ])
        new = make_subscriptions([
            ("CCTV-1", "https://example.com/new1.m3u8"),
            ("CCTV-5", "https://example.com/new5.m3u8"),
        ])

        self.publish(old)
        result = self.publish(new)

        self.assertFalse(result["first_publish"])
        self.assertEqual(
            (self.output_dir / "iptv.m3u").read_text(encoding="utf-8"),
            new["m3u"],
        )
        self.assertEqual(
            (self.output_dir / "iptv.txt").read_text(encoding="utf-8"),
            new["txt"],
        )

    def test_exactly_fifty_percent_route_drop_is_allowed(self):
        """线路数量恰好下降50%，允许发布。"""
        old = four_routes_two_channels()
        new = make_subscriptions([
            ("CCTV-1", "https://example.com/new1.m3u8"),
            ("CCTV-5", "https://example.com/new5.m3u8"),
        ])

        self.publish(old)
        result = self.publish(new)

        self.assertTrue(result["published"])
        self.assertEqual(result["route_count"], 2)

    def test_more_than_fifty_percent_route_drop_is_blocked(self):
        """线路下降保护必须输出新旧数量、比例和未修改状态。"""
        old = four_routes_two_channels()
        new = make_subscriptions([
            ("CCTV-1", "https://example.com/new1.m3u8"),
        ])

        self.publish(old)
        old_m3u = (self.output_dir / "iptv.m3u").read_bytes()
        old_txt = (self.output_dir / "iptv.txt").read_bytes()

        with self.assertRaises(publisher.PublisherError) as context:
            self.publish(new)

        error = context.exception
        self.assertIn("线路数量下降超过50%", str(error))
        self.assertIn("旧线路 4 条", str(error))
        self.assertIn("新线路 1 条", str(error))
        self.assertIn("下降 75.0%", str(error))
        self.assertEqual(error.subscription_state, "UNMODIFIED")

        self.assertEqual(
            (self.output_dir / "iptv.m3u").read_bytes(),
            old_m3u,
        )
        self.assertEqual(
            (self.output_dir / "iptv.txt").read_bytes(),
            old_txt,
        )

    def test_more_than_fifty_percent_channel_drop_is_blocked(self):
        """频道下降保护必须输出新旧数量、比例和未修改状态。"""
        old = make_subscriptions([
            ("CCTV-1", "https://example.com/a1.m3u8"),
            ("CCTV-5", "https://example.com/a5.m3u8"),
            ("CCTV-13", "https://example.com/a13.m3u8"),
            ("CCTV-新闻", "https://example.com/news.m3u8"),
        ])
        new = make_subscriptions([
            ("CCTV-1", "https://example.com/new1.m3u8"),
            ("CCTV-1", "https://example.com/new2.m3u8"),
        ])

        self.publish(old)

        with self.assertRaises(publisher.PublisherError) as context:
            self.publish(new)

        error = context.exception
        self.assertIn("频道数量下降超过50%", str(error))
        self.assertIn("旧频道 4 个", str(error))
        self.assertIn("新频道 1 个", str(error))
        self.assertIn("下降 75.0%", str(error))
        self.assertEqual(error.subscription_state, "UNMODIFIED")

    def test_exactly_fifty_percent_channel_drop_is_allowed(self):
        """频道数量恰好下降50%，允许发布。"""
        old = make_subscriptions([
            ("CCTV-1", "https://example.com/old1.m3u8"),
            ("CCTV-5", "https://example.com/old5.m3u8"),
        ])
        new = make_subscriptions([
            ("CCTV-1", "https://example.com/new1.m3u8"),
        ])

        self.publish(old)
        result = self.publish(new)

        self.assertTrue(result["published"])
        self.assertEqual(result["channel_count"], 1)

    def test_only_one_old_file_exists_is_blocked(self):
        """旧 M3U 和 TXT 只有一份存在时，必须阻止发布。"""
        self.output_dir.mkdir(parents=True)
        (self.output_dir / "iptv.m3u").write_text(
            "#EXTM3U\n",
            encoding="utf-8",
        )

        new = make_subscriptions([
            ("CCTV-1", "https://example.com/new1.m3u8"),
        ])

        with self.assertRaisesRegex(
            publisher.PublisherError,
            "只有一份存在有效内容",
        ) as context:
            self.publish(new)

        self.assertEqual(
            context.exception.subscription_state,
            "UNMODIFIED",
        )

    def test_both_empty_old_files_are_blocked(self):
        """两份旧订阅都存在但内容为空时，必须阻止覆盖。"""
        self.output_dir.mkdir(parents=True)
        (self.output_dir / "iptv.m3u").write_bytes(b"")
        (self.output_dir / "iptv.txt").write_bytes(b"")

        new = make_subscriptions([
            ("CCTV-1", "https://example.com/new1.m3u8"),
        ])

        with self.assertRaisesRegex(
            publisher.PublisherError,
            "旧订阅损坏",
        ) as context:
            self.publish(new)

        self.assertEqual(
            context.exception.subscription_state,
            "UNMODIFIED",
        )

    def test_single_empty_old_file_is_blocked(self):
        """只有一份空的旧订阅文件时，必须阻止覆盖。"""
        self.output_dir.mkdir(parents=True)
        (self.output_dir / "iptv.m3u").write_bytes(b"")

        new = make_subscriptions([
            ("CCTV-1", "https://example.com/new1.m3u8"),
        ])

        with self.assertRaisesRegex(
            publisher.PublisherError,
            "只有一份存在有效内容",
        ) as context:
            self.publish(new)

        self.assertEqual(
            context.exception.subscription_state,
            "UNMODIFIED",
        )

    def test_corrupted_old_subscription_is_blocked(self):
        """旧订阅损坏时，必须阻止覆盖。"""
        self.output_dir.mkdir(parents=True)
        (self.output_dir / "iptv.m3u").write_text(
            "这不是有效的 M3U\n",
            encoding="utf-8",
        )
        (self.output_dir / "iptv.txt").write_text(
            "CCTV-1,https://example.com/old1.m3u8\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            publisher.PublisherError,
            "旧订阅损坏",
        ) as context:
            self.publish(make_subscriptions([
                ("CCTV-1", "https://example.com/new1.m3u8"),
            ]))

        self.assertEqual(
            context.exception.subscription_state,
            "UNMODIFIED",
        )

    def test_old_subscription_read_error_is_unmodified(self):
        """读取旧订阅文件失败时，必须明确标记订阅未修改。"""
        old = make_subscriptions([
            ("CCTV-1", "https://example.com/old1.m3u8"),
            ("CCTV-5", "https://example.com/old5.m3u8"),
        ])
        new = make_subscriptions([
            ("CCTV-1", "https://example.com/new1.m3u8"),
            ("CCTV-5", "https://example.com/new5.m3u8"),
        ])

        self.publish(old)

        with patch(
            "publisher.Path.read_bytes",
            side_effect=PermissionError("模拟旧订阅文件读取权限错误"),
        ):
            with self.assertRaisesRegex(
                publisher.PublisherError,
                "发布前读取旧订阅文件失败",
            ) as context:
                self.publish(new)

        self.assertEqual(
            context.exception.subscription_state,
            "UNMODIFIED",
        )

    def test_second_staged_file_failure_is_unmodified(self):
        """第二份临时文件准备失败时，旧订阅必须保持不变。"""
        old = make_subscriptions([
            ("CCTV-1", "https://example.com/old1.m3u8"),
            ("CCTV-5", "https://example.com/old5.m3u8"),
        ])
        new = make_subscriptions([
            ("CCTV-1", "https://example.com/new1.m3u8"),
            ("CCTV-5", "https://example.com/new5.m3u8"),
        ])

        self.publish(old)

        m3u_path = self.output_dir / "iptv.m3u"
        txt_path = self.output_dir / "iptv.txt"

        old_m3u = m3u_path.read_bytes()
        old_txt = txt_path.read_bytes()

        real_write_temp_file = publisher._write_temp_file
        call_count = {"value": 0}

        def fail_second_staged_file(directory, prefix, data):
            call_count["value"] += 1

            if call_count["value"] == 2:
                raise OSError("模拟第二份临时文件写入失败")

            return real_write_temp_file(directory, prefix, data)

        events = []

        with patch(
            "publisher._write_temp_file",
            side_effect=fail_second_staged_file,
        ):
            with self.assertRaisesRegex(
                publisher.PublisherError,
                "临时文件准备失败",
            ) as context:
                publisher.publish_subscriptions(
                    new,
                    CHANNELS,
                    output_dir=self.output_dir,
                    event_callback=lambda stage, status, detail: (
                        events.append((stage, status, detail))
                    ),
                )

        self.assertEqual(
            context.exception.subscription_state,
            "UNMODIFIED",
        )
        self.assertEqual(m3u_path.read_bytes(), old_m3u)
        self.assertEqual(txt_path.read_bytes(), old_txt)

        self.assertFalse(
            list(self.output_dir.glob(".*.stage-*.tmp"))
        )

        self.assertTrue(
            any(
                stage == "临时文件准备" and status == "FAIL"
                for stage, status, detail in events
            )
        )
    
    def test_failed_second_replace_restores_old_files(self):
        """第二份正式文件替换失败时，必须恢复两份旧文件。"""
        old = make_subscriptions([
            ("CCTV-1", "https://example.com/old1.m3u8"),
            ("CCTV-5", "https://example.com/old5.m3u8"),
        ])
        new = make_subscriptions([
            ("CCTV-1", "https://example.com/new1.m3u8"),
            ("CCTV-5", "https://example.com/new5.m3u8"),
        ])

        self.publish(old)

        m3u_path = self.output_dir / "iptv.m3u"
        txt_path = self.output_dir / "iptv.txt"

        old_m3u = m3u_path.read_bytes()
        old_txt = txt_path.read_bytes()

        real_replace = publisher.os.replace
        call_count = {"value": 0}

        def fail_second_replace(source, target):
            call_count["value"] += 1

            if call_count["value"] == 2:
                raise OSError("模拟第二份文件替换失败")

            return real_replace(source, target)

        with patch(
            "publisher.os.replace",
            side_effect=fail_second_replace,
        ):
            with self.assertRaisesRegex(
                publisher.PublisherError,
                "旧文件已恢复",
            ):
                self.publish(new)

        self.assertEqual(m3u_path.read_bytes(), old_m3u)
        self.assertEqual(txt_path.read_bytes(), old_txt)

    def test_rollback_failure_is_reported(self):
        """回滚失败必须明确标记正式订阅状态。"""

        old = make_subscriptions([
            ("CCTV-1", "https://example.com/old1.m3u8"),
            ("CCTV-5", "https://example.com/old5.m3u8"),
        ])
        new = make_subscriptions([
            ("CCTV-1", "https://example.com/new1.m3u8"),
            ("CCTV-5", "https://example.com/new5.m3u8"),
        ])

        self.publish(old)

        with (
            patch(
                "publisher.os.replace",
                side_effect=OSError("模拟文件替换失败"),
            ),
            patch(
                "publisher._restore_subscription_files",
                return_value=["模拟回滚失败"],
            ),
        ):
            with self.assertRaises(publisher.PublisherError) as context:
                self.publish(new)

        self.assertEqual(
            context.exception.subscription_state,
            "ROLLBACK_FAILED",
        )
        self.assertIn("回滚失败", str(context.exception))   


    def test_post_publish_validation_failure_restores_old_files(self):
        """发布后读取失败时，必须恢复两份旧订阅并记录失败阶段。"""
        old = make_subscriptions([
            ("CCTV-1", "https://example.com/old1.m3u8"),
            ("CCTV-5", "https://example.com/old5.m3u8"),
        ])
        new = make_subscriptions([
            ("CCTV-1", "https://example.com/new1.m3u8"),
            ("CCTV-5", "https://example.com/new5.m3u8"),
        ])

        self.publish(old)

        m3u_path = self.output_dir / "iptv.m3u"
        txt_path = self.output_dir / "iptv.txt"

        old_m3u = m3u_path.read_bytes()
        old_txt = txt_path.read_bytes()

        real_read_text = Path.read_text

        def fail_published_m3u(path, *args, **kwargs):
            if path.name == "iptv.m3u":
                raise OSError("模拟发布后读取失败")

            return real_read_text(path, *args, **kwargs)

        events = []

        with patch(
            "publisher.Path.read_text",
            autospec=True,
            side_effect=fail_published_m3u,
        ):
            with self.assertRaisesRegex(
                publisher.PublisherError,
                "旧文件已恢复",
            ) as context:
                publisher.publish_subscriptions(
                    new,
                    CHANNELS,
                    output_dir=self.output_dir,
                    event_callback=lambda stage, status, detail: (
                        events.append((stage, status, detail))
                    ),
                )

        self.assertEqual(
            context.exception.subscription_state,
            "RESTORED",
        )
        self.assertEqual(m3u_path.read_bytes(), old_m3u)
        self.assertEqual(txt_path.read_bytes(), old_txt)

        self.assertTrue(
            any(
                stage == "发布后校验" and status == "FAIL"
                for stage, status, detail in events
            )
        )
        self.assertTrue(
            any(
                stage == "正式订阅回滚" and status == "COMPLETED"
                for stage, status, detail in events
            )
        )

    def test_restore_continues_after_one_file_restore_failure(self):
        """一份文件恢复失败时，仍必须继续尝试恢复另一份文件。"""
        self.output_dir.mkdir(parents=True)

        m3u_path = self.output_dir / "iptv.m3u"
        txt_path = self.output_dir / "iptv.txt"

        m3u_path.write_bytes(b"new m3u")
        txt_path.write_bytes(b"new txt")

        old_data = [
            b"old m3u",
            b"old txt",
        ]
        targets = [
            m3u_path,
            txt_path,
        ]

        real_write_temp_file = publisher._write_temp_file
        call_count = {"value": 0}

        def fail_first_restore(directory, prefix, data):
            call_count["value"] += 1

            if call_count["value"] == 1:
                raise OSError("模拟第一份文件恢复失败")

            return real_write_temp_file(
                directory,
                prefix,
                data,
            )

        with patch(
            "publisher._write_temp_file",
            side_effect=fail_first_restore,
        ):
            errors = publisher._restore_subscription_files(
                targets,
                old_data,
                self.output_dir,
            )

        self.assertEqual(call_count["value"], 2)
        self.assertEqual(len(errors), 1)
        self.assertIn("iptv.m3u 恢复失败", errors[0])

        self.assertEqual(m3u_path.read_bytes(), b"new m3u")
        self.assertEqual(txt_path.read_bytes(), b"old txt")

    
    def test_inconsistent_route_count_is_rejected(self):
        """传入的线路数量与订阅实际内容不一致时，必须拒绝。"""
        invalid = make_subscriptions([
            ("CCTV-1", "https://example.com/live.m3u8"),
        ])
        invalid["route_count"] = 2

        with self.assertRaisesRegex(
            publisher.PublisherError,
            "线路数量统计不一致",
        ) as context:
            self.publish(invalid)

        self.assertEqual(
            context.exception.subscription_state,
            "UNMODIFIED",
        )

    def test_publish_callback_reports_release_stages(self):
        """发布回调应报告关键阶段，但不改变发布结果。"""
        events = []

        subscriptions = make_subscriptions([
            ("CCTV-1", "https://example.com/cctv1.m3u8"),
            ("CCTV-5", "https://example.com/cctv5.m3u8"),
        ])

        result = publisher.publish_subscriptions(
            subscriptions,
            CHANNELS,
            output_dir=self.output_dir,
            event_callback=lambda stage, status, detail: (
                events.append((stage, status, detail))
            ),
        )

        self.assertTrue(result["published"])

        stages = [event[0] for event in events]

        self.assertIn("发布检查", stages)
        self.assertIn("旧订阅检查", stages)
        self.assertIn("临时文件准备", stages)
        self.assertIn("正式文件替换", stages)
        self.assertIn("发布后校验", stages)
        self.assertIn("发布流程", stages)

    def test_callback_exception_does_not_affect_publish(self):
        """日志回调异常不得阻止正式订阅发布。"""
        subscriptions = make_subscriptions([
            ("CCTV-1", "https://example.com/cctv1.m3u8"),
            ("CCTV-5", "https://example.com/cctv5.m3u8"),
        ])

        def broken_callback(stage, status, detail):
            raise RuntimeError("模拟日志回调异常")

        result = publisher.publish_subscriptions(
            subscriptions,
            CHANNELS,
            output_dir=self.output_dir,
            event_callback=broken_callback,
        )

        self.assertTrue(result["published"])
        self.assertTrue(
            (self.output_dir / "iptv.m3u").is_file()
        )
        self.assertTrue(
            (self.output_dir / "iptv.txt").is_file()
        )


if __name__ == "__main__":
    unittest.main()
