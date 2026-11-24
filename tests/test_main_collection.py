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
from normalizer import _channel_match


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
        "aliases": {
            "CCTV5": "CCTV-5",
            "CCTV5+": "CCTV-5+",
            "CCTV4中文国际": "CCTV-4",
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
        normalize_callback=None,
    ):
        config = {
            "settings": settings,
            "channels": [CHANNEL],
            "manual_sources": [],
            "automatic_sources": [],
        }

        output = io.StringIO()
        errors = io.StringIO()

        def fake_source_batch(
            candidates,
            accepted_routes,
            settings,
            channels,
        ):
            admission = admission_side_effect(
                candidates,
                accepted_routes,
                channels,
                settings,
            )
            return candidates, admission, candidates

        with (
            patch(
                "main.iter_source_contents",
            patch(
                "main._load_historical_candidates",
                return_value=[],
            ),
                return_value=source_stream,
            ),
            patch(
                "main.normalize_source_content",
                side_effect=(
                    normalize_callback
                    if normalize_callback is not None
                    else lambda **kwargs: make_normalized(kwargs["source"])
                ),
            ),
            patch(
                "main.run_source_batch_pipeline",
                side_effect=fake_source_batch,
            ) as admission_mock,
            patch("main._print_detection_summary"),
            patch(
                "main.publisher.build_subscriptions",
                return_value={
                    "route_count": 0,
                    "channel_counts": {CHANNEL: 0},
                    "m3u": "",
                    "txt": "",
                },
            ) as build_mock,
            patch(
                "main.publisher.publish_subscriptions",
                return_value={
                    "published": True,
                    "channel_count": 0,
                    "route_count": 0,
                    "m3u_path": "模拟路径/iptv.m3u",
                    "txt_path": "模拟路径/iptv.txt",
                    "first_publish": True,
                },
            ),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            result = main.run_formal_flow(config)

        return (
            result,
            output.getvalue(),
            errors.getvalue(),
            admission_mock,
            build_mock,
        )

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

    def test_aliases_are_passed_to_normalizer(self):
        received_aliases = []
        settings = make_settings()

        def normalize_callback(**kwargs):
            received_aliases.append(kwargs.get("aliases"))
            return make_normalized(kwargs["source"])

        # main.py 以 settings=... 关键字调用准入函数，
        # 因此模拟函数的参数名必须是 settings。
        def admit(candidates, accepted_routes, channels, settings):
            return self.make_admission(accepted_routes)

        result, _, _, _, _ = self.run_flow(
            iter([make_source_item(0)]),
            settings,
            admit,
            normalize_callback=normalize_callback,
        )

        self.assertEqual(result, 0)
        self.assertEqual(received_aliases, [settings["aliases"]])

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

        self.assertEqual(result, 0)
        self.assertEqual(consumed, [0])
        self.assertEqual(admission_mock.call_count, 1)
        self.assertIn("达到有效来源上限 1", output)
        build_mock.assert_called_once()

    def test_duplicate_source_does_not_consume_valid_source_limit(self):
        consumed = []

        def source_stream():
            for index in range(3):
                consumed.append(index)
                yield make_source_item(index)

        def normalize_callback(**kwargs):
            source = kwargs["source"]

            if source["order"] == 0:
                return {
                    "recognized": True,
                    "format": "m3u",
                    "extracted_candidates": 0,
                    "duplicate_candidates": 1,
                    "matched_target_candidates": 1,
                    "candidates": [],
                }

            return make_normalized(source)

        def admit(candidates, accepted_routes, channels, settings):
            return self.make_admission(accepted_routes)

        result, output, _, admission_mock, build_mock = self.run_flow(
            source_stream(),
            make_settings(max_valid_sources=1),
            admit,
            normalize_callback=normalize_callback,
        )

        self.assertEqual(result, 0)
        self.assertEqual(consumed, [0, 1])
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

        self.assertEqual(result, 0)
        self.assertEqual(consumed, [0])
        self.assertEqual(admission_mock.call_count, 1)
        self.assertIn("达到全部候选总上限 1", output)
        build_mock.assert_called_once()

    def test_all_channels_reached_does_not_stop_next_source(self):
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

        self.assertEqual(result, 0)
        self.assertEqual(consumed, [0, 1, 2])
        self.assertEqual(admission_mock.call_count, 1)
        self.assertNotIn("所有目标频道均已达到", output)
        build_mock.assert_called_once()

    def test_global_batch_receives_all_candidates_once(self):
        consumed = []
        received_candidates = []
        received_routes = []

        def source_stream():
            for index in range(3):
                consumed.append(index)
                yield make_source_item(index)

        def admit(candidates, accepted_routes, channels, settings):
            received_candidates.extend(candidates)
            received_routes.append(list(accepted_routes))
            return self.make_admission(accepted_routes)

        result, output, _, admission_mock, build_mock = self.run_flow(
            source_stream(),
            make_settings(max_valid_sources=2),
            admit,
        )

        self.assertEqual(result, 0)
        self.assertEqual(consumed, [0, 1])
        admission_mock.assert_called_once()

        self.assertEqual(
            [item["normalized_url"] for item in received_candidates],
            [
                "https://example.test/source0.m3u",
                "https://example.test/source1.m3u",
            ],
        )

        self.assertEqual(received_routes, [[]])
        self.assertIn("达到有效来源上限 2", output)
        build_mock.assert_called_once()

class ChannelAliasTests(unittest.TestCase):

    def test_cctv5_alias_matches_canonical_channel(self):
        self.assertEqual(
            _channel_match(["CCTV5"], ["CCTV-5"], {"CCTV5": "CCTV-5"}),
            "CCTV-5",
        )

    def test_cctv5_plus_alias_matches_canonical_channel(self):
        self.assertEqual(
            _channel_match(
                ["CCTV5+"],
                ["CCTV-5+"],
                {"CCTV5+": "CCTV-5+"},
            ),
            "CCTV-5+",
        )

    def test_cctv4_chinese_international_alias_matches_canonical_channel(self):
        self.assertEqual(
            _channel_match(
                ["CCTV4中文国际"],
                ["CCTV-4"],
                {"CCTV4中文国际": "CCTV-4"},
            ),
            "CCTV-4",
        )

    def test_unknown_similar_name_does_not_fuzzy_match(self):
        self.assertIsNone(
            _channel_match(
                ["CCTV5体育高清未知"],
                ["CCTV-5"],
                {"CCTV5": "CCTV-5"},
            )
        )


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

    def test_channel_target_does_not_stop_collection(self):
        self.admission["all_channels_reached"] = True

        reason = main._collection_stop_reason(
            10,
            100,
            self.admission,
            self.limits,
        )

        self.assertIsNone(reason)

    def test_no_limit_reached(self):
        reason = main._collection_stop_reason(
            10,
            100,
            self.admission,
            self.limits,
        )

        self.assertIsNone(reason)


class GlobalpingSummaryTests(unittest.TestCase):

    def test_summary_excludes_not_submitted_candidates(self):
        results = [
            {
                "channel": "CCTV-5",
                "media_type": "HLS",
                "normalized_url": "https://example.test/live.m3u8?token=secret",
                "checks": {
                    "ipv4": {"status": main.PASS},
                    "http": {"status": main.PASS},
                    "hls": {"status": main.PASS},
                    "ts": {"status": main.PASS},
                },
                "globalping": {
                    "status": main.INCOMPLETE,
                    "error": "测量结果未能完成。",
                    "probes": {
                        "probe1": {
                            "country": "CN",
                            "city": "北京",
                            "network": "China Mobile",
                            "asn": 9808,
                        }
                    },
                    "stages": {
                        "main_playlist": {
                            "measurement_id": "test-measurement-1",
                            "status": main.INCOMPLETE,
                            "reason": "测量结果未能完成。",
                        }
                    },
                },
            },
            {
                "channel": "CCTV-6",
                "media_type": "HLS",
                "normalized_url": "https://example.test/other.m3u8",
                "checks": {
                    "ipv4": {"status": main.FAIL},
                    "http": {"status": main.INCOMPLETE},
                    "hls": {"status": main.INCOMPLETE},
                    "ts": {"status": main.INCOMPLETE},
                },
                "globalping": {
                    "status": main.INCOMPLETE,
                    "error": (
                        "本地检测未全部通过，未提交 Globalping；"
                        "不代表远程检测失败。"
                    ),
                    "probes": {},
                    "stages": {},
                },
            },
        ]

        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            main._print_detection_summary(results)

        report = output.getvalue()

        self.assertIn("候选 2 条", report)
        self.assertIn("本地全部通过 1 条", report)
        self.assertIn("Globalping 已提交 1 条", report)
        self.assertIn("未提交 1 条", report)
        self.assertIn("实际远程结果 PASS 0 条", report)
        self.assertIn("INCOMPLETE 1 条", report)

        self.assertIn("北京", report)
        self.assertIn("China Mobile", report)
        self.assertIn("test-measurement-1", report)
        self.assertIn("测量结果未能完成", report)

        self.assertNotIn("token=secret", report)


if __name__ == "__main__":
    unittest.main()
