"""正式运行异常退出与发布流程保护测试。"""

import contextlib
import io
import unittest
from unittest.mock import patch

import main
import publisher


CHANNEL = "CCTV-5"


def make_config():
    return {
        "settings": {
            "collection": {
                "max_valid_sources": 40,
                "max_candidates_per_source": 100,
                "max_candidates_total": 1000,
                "max_index_links_per_page": 40,
            },
            "detection": {
                "max_hls_manifest_bytes": 1048576,
            },
            "globalping": {
                "max_probes": 3,
            },
            "selection": {
                "max_routes_per_channel": 2,
            },
        },
        "channels": [CHANNEL],
        "manual_sources": [],
        "automatic_sources": [],
    }


def make_source_item():
    url = "https://example.test/source.m3u"

    return {
        "source": {
            "name": "模拟来源",
            "url": url,
            "priority": 0,
            "order": 0,
        },
        "download": {
            "final_url": url,
            "content": "模拟内容",
            "content_type": "text/plain",
        },
        "errors": [],
    }


def make_candidate():
    return {
        "channel": CHANNEL,
        "normalized_url": "https://example.test/live.m3u8",
        "media_type": "HLS",
    }


class MainFormalFailureTests(unittest.TestCase):

    def run_quietly(self, config):
        output = io.StringIO()
        errors = io.StringIO()

        with (
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            result = main.run_formal_flow(config)

        return result, output.getvalue(), errors.getvalue()

    def test_admission_error_stops_before_subscription_generation(self):
        """准入系统异常时，不得继续生成或发布订阅。"""

        config = make_config()

        with (
            patch(
                "main.iter_source_contents",
                return_value=iter([make_source_item()]),
            ),
            patch(
                "main._load_historical_candidates",
                return_value=[],
            ),
            patch(
                "main.normalize_source_content",
                return_value={
                    "recognized": True,
                    "format": "m3u",
                    "extracted_candidates": 1,
                    "duplicate_candidates": 0,
                    "matched_target_candidates": 1,
                    "candidates": [make_candidate()],
                },
            ),
            patch(
                "main.run_source_batch_pipeline",
                side_effect=publisher.PublisherError(
                    "模拟准入异常"
                ),
            ),
            patch("main.publisher.build_subscriptions") as build_mock,
            patch("main.publisher.publish_subscriptions") as publish_mock,
        ):
            result, _, errors = self.run_quietly(config)

        self.assertEqual(result, 1)
        build_mock.assert_not_called()
        publish_mock.assert_not_called()
        self.assertIn("正式订阅未修改", errors)

    def test_subscription_build_error_stops_before_publish(self):
        """订阅生成失败时，不得执行正式发布。"""

        config = make_config()

        with (
            patch(
                "main.iter_source_contents",
                return_value=iter([]),
            ),
            patch(
                "main._load_historical_candidates",
                return_value=[],
            ),
            patch(
                "main.publisher.build_subscriptions",
                side_effect=publisher.PublisherError(
                    "模拟订阅生成异常"
                ),
            ),
            patch("main.publisher.publish_subscriptions") as publish_mock,
        ):
            result, _, errors = self.run_quietly(config)

        self.assertEqual(result, 1)
        publish_mock.assert_not_called()
        self.assertIn("正式订阅未修改", errors)

    def test_publish_error_returns_failure(self):
        """正式发布异常时，程序必须返回失败。"""

        config = make_config()

        subscriptions = {
            "route_count": 0,
            "channel_counts": {CHANNEL: 0},
            "m3u": "#EXTM3U\n",
            "txt": "",
        }

        with (
            patch(
                "main.iter_source_contents",
                return_value=iter([]),
            ),
            patch(
                "main._load_historical_candidates",
                return_value=[],
            ),
            patch(
                "main.publisher.build_subscriptions",
                return_value=subscriptions,
            ),
            patch(
                "main.publisher.publish_subscriptions",
                side_effect=OSError("模拟文件替换异常"),
            ) as publish_mock,
        ):
            result, _, errors = self.run_quietly(config)

        self.assertEqual(result, 1)
        publish_mock.assert_called_once()
        self.assertIn(
            "｜订阅发布｜正式订阅｜FAIL｜模拟文件替换异常",
            errors,
        )
        self.assertIn(
            "｜订阅发布｜正式订阅文件｜UNKNOWN｜正式订阅状态无法确认",
            errors,
        )

    def test_admission_results_are_printed_individually(self):
        """最终准入日志必须逐条输出状态和原因。"""

        results = [
            {
                "candidate": {
                    "channel": CHANNEL,
                    "normalized_url": "https://example.test/a.m3u8",
                },
                "status": publisher.ACCEPTED,
                "reason": "检测通过",
            },
            {
                "candidate": {
                    "channel": CHANNEL,
                    "normalized_url": "https://example.test/b.m3u8",
                },
                "status": publisher.REJECTED,
                "reason": "质量排名未进入最终两条线路",
            },
        ]

        output = io.StringIO()
        errors = io.StringIO()

        with (
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            main._print_admission_results(results)

        content = output.getvalue() + errors.getvalue()

        self.assertIn("第 1 条", content)
        self.assertIn("第 2 条", content)
        self.assertIn("ACCEPTED", content)
        self.assertIn("REJECTED", content)
        self.assertIn("质量排名未进入最终两条线路", content)

if __name__ == "__main__":
    unittest.main()
