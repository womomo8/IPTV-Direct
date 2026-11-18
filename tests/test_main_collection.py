
"""main.py 跨来源候选池、统一检测和优选测试。

全部使用模拟数据：
不访问真实来源、不执行真实线路检测、
不调用 Globalping API、不消耗额度、不修改正式订阅。
"""

import contextlib
import io
import unittest
from unittest.mock import patch

import main

from normalizer import _channel_match, normalize_source_content


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


def make_source_item(index, source_type="automatic"):
    url = f"https://example.test/source{index}.m3u"

    priority = 0 if source_type == "manual" else 1

    return {
        "source": {
            "name": f"模拟来源{index}",
            "url": url,
            "source_type": source_type,
            "priority": priority,
            "order": index,
        },
        "download": {
            "final_url": url,
            "content": f"模拟来源内容{index}",
            "content_type": "text/plain",
            "http_status": 200,
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
                "priority": source_info.get("priority", 1),
                "source_order": source_info.get("order", 0),
                "source_type": source_info.get("source_type", "automatic"),
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
        detection_inputs = []

        def fake_detection(candidates, current_settings, channels=None):
            detection_inputs.append({
                "candidates": list(candidates),
                "settings": current_settings,
                "channels": channels,
            })
            return list(candidates)

        def fake_normalize(**kwargs):
            if normalize_callback is not None:
                return normalize_callback(**kwargs)
            return make_normalized(kwargs["source"])

        with (
            patch(
                "main.iter_source_contents",
                return_value=source_stream,
            ),
            patch(
                "main.normalize_source_content",
                side_effect=fake_normalize,
            ),
            patch(
                "main.run_detection_pipeline",
                side_effect=fake_detection,
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
            detection_inputs,
        )

    @staticmethod
    def make_admission(
        accepted_routes,
        all_channels_reached=False,
        count=0,
        candidates=None,
    ):
        return {
            "accepted_routes": accepted_routes,
            "channel_counts": {CHANNEL: count},
            "all_channels_reached": all_channels_reached,
            "accepted_count": 0,
            "rejected_count": 0,
            "results": [],
        }

    def test_aliases_are_passed_to_normalizer(self):
        received_aliases = []
        settings = make_settings()

        def normalize_callback(**kwargs):
            received_aliases.append(kwargs.get("aliases"))
            return make_normalized(kwargs["source"])

        def admit(candidates, accepted_routes, channels, settings):
            return self.make_admission(accepted_routes)

        result, _, _, _, _, _ = self.run_flow(
            iter([make_source_item(0)]),
            settings,
            admit,
            normalize_callback=normalize_callback,
        )

        self.assertEqual(result, 0)
        self.assertEqual(
            received_aliases,
            [settings["aliases"]],
        )

    def test_all_sources_are_collected_before_detection(self):
        consumed = []

        def source_stream():
            for index in range(3):
                consumed.append(index)
                yield make_source_item(index)

        def normalize_callback(**kwargs):
            return make_normalized(kwargs["source"])

        def admit(candidates, accepted_routes, channels, settings):
            return self.make_admission(accepted_routes)

        result, _, _, admission_mock, _, detection_inputs = self.run_flow(
            source_stream(),
            make_settings(),
            admit,
            normalize_callback=normalize_callback,
        )

        self.assertEqual(result, 0)
        self.assertEqual(consumed, [0, 1, 2])

        # 所有来源处理完毕后，只统一检测一次。
        self.assertEqual(len(detection_inputs), 1)
        self.assertEqual(
            len(detection_inputs[0]["candidates"]),
            3,
        )

        # publisher 也只在完整候选池形成后调用一次。
        self.assertEqual(admission_mock.call_count, 1)
        self.assertEqual(
            len(admission_mock.call_args.kwargs["candidates"]),
            3,
        )

    def test_valid_source_limit_stops_before_next_source(self):
        consumed = []

        def source_stream():
            for index in range(3):
                consumed.append(index)
                yield make_source_item(index)

        def admit(candidates, accepted_routes, channels, settings):
            return self.make_admission(accepted_routes)

        result, output, _, admission_mock, build_mock, detection_inputs = (
            self.run_flow(
                source_stream(),
                make_settings(max_valid_sources=1),
                admit,
            )
        )

        self.assertEqual(result, 0)
        self.assertEqual(consumed, [0])
        self.assertEqual(admission_mock.call_count, 1)
        self.assertEqual(len(detection_inputs), 1)
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

        result, output, _, admission_mock, build_mock, detection_inputs = (
            self.run_flow(
                source_stream(),
                make_settings(max_candidates_total=1),
                admit,
            )
        )

        self.assertEqual(result, 0)
        self.assertEqual(consumed, [0])
        self.assertEqual(admission_mock.call_count, 1)
        self.assertEqual(len(detection_inputs), 1)
        self.assertIn("达到全部候选总上限 1", output)
        build_mock.assert_called_once()

    def test_channel_target_does_not_interrupt_collection_early(self):
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

        result, _, _, admission_mock, _, detection_inputs = self.run_flow(
            source_stream(),
            make_settings(),
            admit,
        )

        self.assertEqual(result, 0)

        # 频道目标只能在完整候选池统一检测和准入后判断，
        # 不能再像旧流程一样处理一个来源就提前停止采集。
        self.assertEqual(consumed, [0, 1, 2])
        self.assertEqual(len(detection_inputs), 1)
        self.assertEqual(admission_mock.call_count, 1)

    def test_publisher_receives_one_complete_candidate_batch(self):
        received = []

        def source_stream():
            for index in range(4):
                yield make_source_item(index)

        def admit(candidates, accepted_routes, channels, settings):
            received.append({
                "candidates": list(candidates),
                "accepted_routes": list(accepted_routes),
                "channels": list(channels),
            })
            return self.make_admission(accepted_routes)

        result, _, _, admission_mock, _, _ = self.run_flow(
            source_stream(),
            make_settings(),
            admit,
        )

        self.assertEqual(result, 0)
        self.assertEqual(admission_mock.call_count, 1)
        self.assertEqual(len(received), 1)
        self.assertEqual(len(received[0]["candidates"]), 4)
        self.assertEqual(received[0]["accepted_routes"], [])
        self.assertEqual(received[0]["channels"], [CHANNEL])

    def test_source_failure_does_not_stop_later_sources(self):
        def source_stream():
            yield {
                "source": {
                    "name": "失败来源",
                    "url": "https://example.test/fail.m3u",
                    "source_type": "automatic",
                },
                "download": None,
                "errors": ["模拟下载失败"],
            }
            yield make_source_item(1)

        def admit(candidates, accepted_routes, channels, settings):
            return self.make_admission(accepted_routes)

        result, _, errors, admission_mock, _, detection_inputs = self.run_flow(
            source_stream(),
            make_settings(),
            admit,
        )

        self.assertEqual(result, 0)
        self.assertIn("模拟下载失败", errors)
        self.assertEqual(len(detection_inputs), 1)
        self.assertEqual(len(detection_inputs[0]["candidates"]), 1)
        self.assertEqual(admission_mock.call_count, 1)


class ChannelAliasTests(unittest.TestCase):

    def test_cctv5_alias_matches_canonical_channel(self):
        self.assertEqual(
            _channel_match(
                ["CCTV5"],
                ["CCTV-5"],
                {"CCTV5": "CCTV-5"},
            ),
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

    def test_real_configuration_accepts_unselected_channel_aliases(self):
        config = main.load_configuration(root=main.ROOT)

        self.assertIn("13873", config["settings"]["aliases"])
        self.assertEqual(
            config["settings"]["aliases"]["13873"],
            "安徽卫视",
        )
        self.assertNotIn("安徽卫视", config["channels"])

    def test_verified_real_source_aliases_match_configured_channels(self):
        config = main.load_configuration(root=main.ROOT)
        aliases = config["settings"]["aliases"]

        expected_aliases = {
            "Jade (1080p)": "翡翠台",
            "Jade.hk@SD": "翡翠台",
            "PhoenixInfoNewsChannel.hk@SD": "凤凰资讯台",
            "凤凰资讯": "凤凰资讯台",
            "Lotus TV": "澳门莲花卫视",
            "LotusTV.mo@SD": "澳门莲花卫视",
            "睛彩篮球「移动」": "睛彩篮球",
            "睛彩竞技「移动」": "睛彩竞技",
        }

        for alias, canonical in expected_aliases.items():
            with self.subTest(alias=alias):
                self.assertEqual(aliases.get(alias), canonical)
                self.assertEqual(
                    _channel_match(
                        [alias],
                        config["channels"],
                        aliases,
                    ),
                    canonical,
                )

    def test_selected_channel_alias_matches_and_enters_candidate_pool(self):
        result = normalize_source_content(
            content=(
                '#EXTM3U\n'
                '#EXTINF:-1 tvg-name="13873",13873\n'
                'https://example.com/anhui.m3u8\n'
            ),
            source={
                "url": "https://source.example/list.m3u",
                "final_url": "https://source.example/list.m3u",
                "source_type": "automatic",
                "priority": 1,
                "order": 0,
            },
            channels=["安徽卫视"],
            max_candidates_per_source=10,
            seen_urls=set(),
            aliases={"13873": "安徽卫视"},
        )

        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(
            result["candidates"][0]["channel"],
            "安徽卫视",
        )

    def test_unselected_channel_alias_is_excluded_from_candidate_pool(self):
        seen_urls = set()

        result = normalize_source_content(
            content=(
                '#EXTM3U\n'
                '#EXTINF:-1 tvg-name="13873",13873\n'
                'https://example.com/anhui.m3u8\n'
            ),
            source={
                "url": "https://source.example/list.m3u",
                "final_url": "https://source.example/list.m3u",
                "source_type": "automatic",
                "priority": 1,
                "order": 0,
            },
            channels=["CCTV-1"],
            max_candidates_per_source=10,
            seen_urls=seen_urls,
            aliases={"13873": "安徽卫视"},
        )

        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["matched_target_candidates"], 0)
        self.assertEqual(seen_urls, set())


if __name__ == "__main__":
    unittest.main()
