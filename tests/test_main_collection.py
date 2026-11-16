
"""main.py 来源批次调度、停止条件及 publisher 交接测试。

全部使用模拟数据，不访问真实来源，不执行真实线路检测，
不调用 Globalping API，不修改正式订阅。
"""

import contextlib
import io
import unittest
from unittest.mock import patch

import main
import publisher


CHANNEL = "模拟测试频道"


def make_settings(
    max_valid_sources=40,
    max_candidates_total=1000,
):
    return {
        "collection": {
            "max_valid_sources": max_valid_sources,
            "max_candidates_per_source": 100,
            "max_candidates_total": max_candidates_total,
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
    }


def make_source_item(index):
    url = f"https://example.test/source{index}.m3u"

    return {
        "source": {
            "name": f"模拟来源{index}",
            "url": url,
            "priority": 0,
            "order": index,
        },
        "download": {
            "final_url": url,
            "content": "模拟来源内容",
            "content_type": "text/plain",
        },
        "errors": [],
    }


def make_normalized(source_info):
    url = source_info["final_url"]

    return {
        "recognized": True,
        "format": "m3u",
        "extracted_candidates": 1,
        "duplicate_candidates": 0,
        "matched_target_candidates": 1,
        "candidates": [
            {
                "channel": CHANNEL,
                "normalized_url": url,
                "media_type": "HLS",
            }
        ],
    }


class MainCollectionFlowTests(unittest.TestCase):

    def run_flow(
        self,
        source_stream,
        settings,
        admission_side_effect,
    ):
        config = {
            "settings": settings,
            "channels": [CHANNEL],
            "manual_sources": [],
            "automatic_sources": [],
        }

        output = io.StringIO()
        errors = io.StringIO()

        with (
            patch(
                "main.iter_source_contents",
                return_value=source_stream,
            ),
            patch(
                "main.normalize_source_content",
                side_effect=lambda **kwargs: make_normalized(
                    kwargs["source"]
                ),
            ),
            patch(
                "main.run_detection_pipeline",
                side_effect=lambda candidates, current_settings: candidates,
            ),
            patch("main._print_detection_summary"),
            patch(
                "main.process_admission_batch",
                side_effect=admission_side_effect,
            ) as admission_mock,
            patch(
                "main.publisher.build_subscriptions",
                return_value={
                    "route_count": 0,
                    "channel_counts": {CHANNEL: 0},
                    "m3u": "",
                    "txt": "",
                },
            ) as build_mock,
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            result = main.run_formal_flow(config)

        return result, output.getvalue(), errors.getvalue(), admission_mock, build_mock

    @staticmethod
    def make_admission(
        accepted_routes,
        all_channels_reached=False,
        count=0,
    ):
        return {
            "accepted_routes": accepted_routes,
            "channel_counts": {CHANNEL: count},
            "all_channels_reached": all_channels_reached,
            "results": [],
        }

    def test_valid_source_limit_stops_before_next_source(self):
        consumed = []

        def source_stream():
            for index in range(3):
                consumed.append(index)
                yield make_source_item(index)

        def admit(candidates, accepted_routes, channels, settings):
            return self.make_admission(
                accepted_routes,
                count=0,
            )

        result, output, _, admission_mock, build_mock = self.run_flow(
            source_stream(),
            make_settings(max_valid_sources=1),
            admit,
        )

        self.assertEqual(result, 2)
        self.assertEqual(consumed, [0])
        self.assertEqual(admission_mock.call_count, 1)
        self.assertIn("达到有效来源上限 1", output)
        build_mock.assert_called_once()

    def test_candidate_total_limit_stops_before_next_source(self):
        consumed = []

        def source_stream():
            for index in range(3):
                consumed.append(index)
                yield make_source_item(index)

        def admit(candidates, accepted_routes, channels, settings):
            return self.make_admission(accepted_routes)

        result, output, _, admission_mock, build_mock = self.run_flow(
            source_stream(),
            make_settings(max_candidates_total=1),
            admit,
        )

        self.assertEqual(result, 2)
        self.assertEqual(consumed, [0])
        self.assertEqual(admission_mock.call_count, 1)
        self.assertIn("达到全部候选总上限 1", output)
        build_mock.assert_called_once()

    def test_all_channels_reached_stops_before_next_source(self):
        consumed = []

        def source_stream():
            for index in range(3):
                consumed.append(index)
                yield make_source_item(index)

        def admit(candidates, accepted_routes, channels, settings):
            return self.make_admission(
                accepted_routes,
                all_channels_reached=True,
                count=2,
            )

        result, output, _, admission_mock, build_mock = self.run_flow(
            source_stream(),
            make_settings(),
            admit,
        )

        self.assertEqual(result, 2)
        self.assertEqual(consumed, [0])
        self.assertEqual(admission_mock.call_count, 1)
        self.assertIn("所有目标频道均已达到", output)
        build_mock.assert_called_once()

    def test_next_batch_receives_previous_admitted_routes(self):
        consumed = []
        received_routes = []

        def source_stream():
            for index in range(3):
                consumed.append(index)
                yield make_source_item(index)

        def admit(candidates, accepted_routes, channels, settings):
            received_routes.append(list(accepted_routes))

            new_routes = accepted_routes + [
                {"batch": len(received_routes)}
            ]

            return self.make_admission(
                new_routes,
                count=len(new_routes),
            )

        result, output, _, admission_mock, build_mock = self.run_flow(
            source_stream(),
            make_settings(max_valid_sources=2),
            admit,
        )

        self.assertEqual(result, 2)
        self.assertEqual(consumed, [0, 1])
        self.assertEqual(admission_mock.call_count, 2)

        self.assertEqual(
            received_routes,
            [
                [],
                [{"batch": 1}],
            ],
        )

        self.assertIn("达到有效来源上限 2", output)
        build_mock.assert_called_once()


class CollectionStopReasonTests(unittest.TestCase):

    def setUp(self):
        self.limits = {
            "max_valid_sources": 40,
            "max_candidates_total": 1000,
        }

        self.admission = {
            "all_channels_reached": False,
        }

    def test_valid_source_limit(self):
        reason = main._collection_stop_reason(
            40,
            100,
            self.admission,
            self.limits,
        )

        self.assertEqual(reason, "达到有效来源上限 40")

    def test_candidate_total_limit(self):
        reason = main._collection_stop_reason(
            10,
            1000,
            self.admission,
            self.limits,
        )

        self.assertEqual(reason, "达到全部候选总上限 1000")

    def test_channel_target_limit(self):
        self.admission["all_channels_reached"] = True

        reason = main._collection_stop_reason(
            10,
            100,
            self.admission,
            self.limits,
        )

        self.assertEqual(
            reason,
            "所有目标频道均已达到每频道 2 条有效线路目标",
        )

    def test_no_limit_reached(self):
        reason = main._collection_stop_reason(
            10,
            100,
            self.admission,
            self.limits,
        )

        self.assertIsNone(reason)


if __name__ == "__main__":
    unittest.main()
