"""IPTV-Direct：publisher.py 线路准入与订阅生成专项测试。"""

import unittest

import main
import publisher


CHANNELS = ["CCTV-1", "CCTV-5"]

SETTINGS = {
    "selection": {
        "max_routes_per_channel": 2,
    }
}


def make_candidate(
    url="https://example.com/live.m3u8",
    channel="CCTV-1",
    priority=1,
    source_order=0,
    media_type="HLS",
    local_status="PASS",
    global_status="PASS",
    quality=None,
):
    """构造完整的线路检测结果。"""

    local_hls = (
        "N/A" if media_type == "DIRECT_TS" else local_status
    )
    remote_hls = (
        "N/A" if media_type == "DIRECT_TS" else global_status
    )

    local_checks = {
        "ipv4": {"status": local_status},
        "http": {"status": local_status},
        "hls": {"status": local_hls},
        "ts": {"status": local_status},
    }

    remote_checks = {
        "ipv4": {"status": global_status},
        "http": {"status": global_status},
        "hls": {"status": remote_hls},
        "ts": {"status": global_status},
    }

    return {
        "channel": channel,
        "normalized_url": url,
        "media_type": media_type,
        "priority": priority,
        "source_order": source_order,
        "source_type": "manual" if priority == 0 else "automatic",
        "checks": local_checks,
        "globalping": {
            "status": global_status,
            "checks": remote_checks,
        },
        "quality": quality,
    }


class PublisherAdmissionTests(unittest.TestCase):

    def process(self, candidates, accepted_routes=None):
        return publisher.process_batch(
            candidates=candidates,
            accepted_routes=accepted_routes or [],
            channels=CHANNELS,
            settings=SETTINGS,
        )

    def test_hls_all_checks_pass_is_accepted(self):
        result = self.process([make_candidate()])

        self.assertEqual(len(result["accepted_routes"]), 1)
        self.assertEqual(
            result["results"][0]["status"],
            publisher.ACCEPTED,
        )

    def test_direct_ts_with_hls_na_is_accepted(self):
        candidate = make_candidate(
            url="https://example.com/live.ts",
            media_type="DIRECT_TS",
        )

        result = self.process([candidate])

        self.assertEqual(len(result["accepted_routes"]), 1)
        self.assertEqual(
            result["accepted_routes"][0]["checks"]["hls"]["status"],
            "N/A",
        )

    def test_local_failure_is_rejected(self):
        result = self.process([
            make_candidate(local_status="FAIL")
        ])

        self.assertEqual(len(result["accepted_routes"]), 0)
        self.assertEqual(
            result["results"][0]["status"],
            publisher.REJECTED,
        )

    def test_globalping_failure_is_rejected(self):
        result = self.process([
            make_candidate(global_status="FAIL")
        ])

        self.assertEqual(len(result["accepted_routes"]), 0)
        self.assertEqual(
            result["results"][0]["status"],
            publisher.REJECTED,
        )

    def test_globalping_incomplete_can_be_accepted(self):
        candidate = make_candidate(global_status="INCOMPLETE")

        candidate["globalping"]["checks"]["ipv4"]["status"] = "PASS"
        candidate["globalping"]["checks"]["http"]["status"] = "INCOMPLETE"
        candidate["globalping"]["checks"]["hls"]["status"] = "INCOMPLETE"
        candidate["globalping"]["checks"]["ts"]["status"] = "PASS"

        result = self.process([candidate])

        self.assertEqual(len(result["accepted_routes"]), 1)
        self.assertEqual(
            result["accepted_routes"][0]["globalping"]["status"],
            "INCOMPLETE",
        )

    def test_globalping_pass_with_incomplete_check_is_rejected(self):
        candidate = make_candidate()
        candidate["globalping"]["checks"]["http"]["status"] = "INCOMPLETE"

        result = self.process([candidate])

        self.assertEqual(len(result["accepted_routes"]), 0)
        self.assertEqual(
            result["results"][0]["status"],
            publisher.REJECTED,
        )

    def test_globalping_incomplete_with_failed_check_is_rejected(self):
        candidate = make_candidate(global_status="INCOMPLETE")
        candidate["globalping"]["checks"]["http"]["status"] = "FAIL"

        result = self.process([candidate])

        self.assertEqual(len(result["accepted_routes"]), 0)
        self.assertEqual(
            result["results"][0]["status"],
            publisher.REJECTED,
        )

    def test_unknown_media_type_is_rejected(self):
        candidate = make_candidate()
        candidate["media_type"] = "UNKNOWN"

        result = self.process([candidate])

        self.assertEqual(len(result["accepted_routes"]), 0)
        self.assertEqual(
            result["results"][0]["status"],
            publisher.REJECTED,
        )

    def test_duplicate_url_is_not_accepted_twice(self):
        first = make_candidate(
            url="https://example.com/same.m3u8",
            source_order=0,
        )
        second = make_candidate(
            url="https://example.com/same.m3u8",
            source_order=1,
        )

        result = self.process([first, second])

        self.assertEqual(len(result["accepted_routes"]), 1)
        self.assertEqual(result["channel_counts"]["CCTV-1"], 1)

    def test_results_preserve_original_candidate_order(self):
        invalid = make_candidate(
            url="https://example.com/invalid.m3u8",
            local_status="FAIL",
        )
        first = make_candidate(
            url="https://example.com/first.m3u8",
        )
        duplicate = make_candidate(
            url="https://example.com/first.m3u8",
            source_order=1,
        )
        second = make_candidate(
            url="https://example.com/second.m3u8",
            source_order=2,
        )

        result = self.process([
            invalid,
            first,
            duplicate,
            second,
        ])

        self.assertEqual(
            [
                item["candidate"]["normalized_url"]
                for item in result["results"]
            ],
            [
                "https://example.com/invalid.m3u8",
                "https://example.com/first.m3u8",
                "https://example.com/first.m3u8",
                "https://example.com/second.m3u8",
            ],
        )
        self.assertEqual(
            [item["status"] for item in result["results"]],
            [
                publisher.REJECTED,
                publisher.ACCEPTED,
                publisher.REJECTED,
                publisher.ACCEPTED,
            ],
        )

    def test_channel_keeps_at_most_two_routes(self):
        candidates = [
            make_candidate(
                url="https://example.com/auto.m3u8",
                priority=1,
                source_order=0,
            ),
            make_candidate(
                url="https://example.com/manual-1.m3u8",
                priority=0,
                source_order=0,
            ),
            make_candidate(
                url="https://example.com/manual-2.m3u8",
                priority=0,
                source_order=1,
            ),
        ]

        result = self.process(candidates)

        self.assertEqual(len(result["accepted_routes"]), 2)
        self.assertEqual(result["channel_counts"]["CCTV-1"], 2)

        self.assertEqual(
            [
                item["normalized_url"]
                for item in result["accepted_routes"]
            ],
            [
                "https://example.com/auto.m3u8",
                "https://example.com/manual-1.m3u8",
            ],
        )

    def test_missing_quality_preserves_original_candidate_order(self):
        candidates = [
            make_candidate(
                url="https://example.com/automatic.m3u8",
                priority=1,
                source_order=0,
            ),
            make_candidate(
                url="https://example.com/manual.m3u8",
                priority=0,
                source_order=0,
            ),
        ]

        result = self.process(candidates)

        self.assertEqual(
            [
                item["normalized_url"]
                for item in result["accepted_routes"]
            ],
            [
                "https://example.com/automatic.m3u8",
                "https://example.com/manual.m3u8",
            ],
        )
            def test_better_automatic_route_beats_manual_route(self):
        candidates = [
            make_candidate(
                url="https://example.com/manual-slow.m3u8",
                priority=0,
                quality={
                    "request_count": 3,
                    "request_success_count": 2,
                    "timeout_count": 1,
                    "total_elapsed_seconds": 12.0,
                    "total_read_seconds": 9.0,
                    "total_bytes_read": 1000,
                    "segment_total": 3,
                    "segment_success_count": 1,
                },
            ),
            make_candidate(
                url="https://example.com/automatic-fast.m3u8",
                priority=1,
                quality={
                    "request_count": 3,
                    "request_success_count": 3,
                    "timeout_count": 0,
                    "total_elapsed_seconds": 3.0,
                    "total_read_seconds": 1.5,
                    "total_bytes_read": 3000,
                    "segment_total": 3,
                    "segment_success_count": 3,
                },
            ),
        ]

        result = self.process(candidates)

        self.assertEqual(
            result["accepted_routes"][0]["normalized_url"],
            "https://example.com/automatic-fast.m3u8",
        )

    def test_better_manual_route_beats_automatic_route(self):
        candidates = [
            make_candidate(
                url="https://example.com/automatic-slow.m3u8",
                priority=1,
                quality={
                    "request_count": 3,
                    "request_success_count": 2,
                    "timeout_count": 1,
                    "total_elapsed_seconds": 12.0,
                    "total_read_seconds": 9.0,
                    "total_bytes_read": 1000,
                    "segment_total": 3,
                    "segment_success_count": 1,
                },
            ),
            make_candidate(
                url="https://example.com/manual-fast.m3u8",
                priority=0,
                quality={
                    "request_count": 3,
                    "request_success_count": 3,
                    "timeout_count": 0,
                    "total_elapsed_seconds": 3.0,
                    "total_read_seconds": 1.5,
                    "total_bytes_read": 3000,
                    "segment_total": 3,
                    "segment_success_count": 3,
                },
            ),
        ]

        result = self.process(candidates)

        self.assertEqual(
            result["accepted_routes"][0]["normalized_url"],
            "https://example.com/manual-fast.m3u8",
        )

    def test_hls_and_direct_ts_are_ranked_independently(self):
        candidates = [
            make_candidate(
                url="https://example.com/hls-low-segment.m3u8",
                media_type="HLS",
                quality={
                    "request_count": 3,
                    "request_success_count": 3,
                    "timeout_count": 0,
                    "total_elapsed_seconds": 1.0,
                    "total_read_seconds": 0.5,
                    "total_bytes_read": 3000,
                    "segment_total": 3,
                    "segment_success_count": 1,
                },
            ),
            make_candidate(
                url="https://example.com/direct-slow.ts",
                media_type="DIRECT_TS",
                quality={
                    "request_count": 3,
                    "request_success_count": 2,
                    "timeout_count": 1,
                    "total_elapsed_seconds": 9.0,
                    "total_read_seconds": 6.0,
                    "total_bytes_read": 1000,
                    "segment_total": 0,
                    "segment_success_count": 0,
                },
            ),
            make_candidate(
                url="https://example.com/hls-high-segment.m3u8",
                media_type="HLS",
                quality={
                    "request_count": 3,
                    "request_success_count": 2,
                    "timeout_count": 1,
                    "total_elapsed_seconds": 9.0,
                    "total_read_seconds": 6.0,
                    "total_bytes_read": 1000,
                    "segment_total": 3,
                    "segment_success_count": 3,
                },
            ),
            make_candidate(
                url="https://example.com/direct-fast.ts",
                media_type="DIRECT_TS",
                quality={
                    "request_count": 3,
                    "request_success_count": 3,
                    "timeout_count": 0,
                    "total_elapsed_seconds": 1.0,
                    "total_read_seconds": 0.5,
                    "total_bytes_read": 3000,
                    "segment_total": 0,
                    "segment_success_count": 0,
                },
            ),
        ]

        result = self.process(candidates)

        urls = [
            item["normalized_url"]
            for item in result["accepted_routes"]
        ]

        # HLS 只在 HLS 的原位置槽内按分片质量排序。
        # DIRECT_TS 只在 DIRECT_TS 的原位置槽内按通用质量排序。
        # 两种媒体类型不互相交换位置。
        self.assertEqual(
            urls,
            [
                "https://example.com/hls-high-segment.m3u8",
                "https://example.com/direct-fast.ts",
                "https://example.com/hls-low-segment.m3u8",
                "https://example.com/direct-slow.ts",
            ],
        )

    def test_existing_routes_compete_with_new_candidate_by_quality(self):
        old_slowest = make_candidate(
            url="https://example.com/old-slowest.m3u8",
            quality={
                "request_count": 3,
                "request_success_count": 1,
                "timeout_count": 2,
                "total_elapsed_seconds": 15.0,
                "total_read_seconds": 12.0,
                "total_bytes_read": 500,
                "segment_total": 3,
                "segment_success_count": 1,
            },
        )
        old_slowest["selection"] = {"status": publisher.ACCEPTED}

        old_middle = make_candidate(
            url="https://example.com/old-middle.m3u8",
            quality={
                "request_count": 3,
                "request_success_count": 2,
                "timeout_count": 1,
                "total_elapsed_seconds": 8.0,
                "total_read_seconds": 5.0,
                "total_bytes_read": 1500,
                "segment_total": 3,
                "segment_success_count": 2,
            },
        )
        old_middle["selection"] = {"status": publisher.ACCEPTED}

        new_route = make_candidate(
            url="https://example.com/new-fast.m3u8",
            quality={
                "request_count": 3,
                "request_success_count": 3,
                "timeout_count": 0,
                "total_elapsed_seconds": 3.0,
                "total_read_seconds": 1.5,
                "total_bytes_read": 3000,
                "segment_total": 3,
                "segment_success_count": 3,
            },
        )

        result = self.process(
            [new_route],
            accepted_routes=[old_slowest, old_middle],
        )

        # 每频道最多两条：新快线路挤掉历史最差线路，保留历史次优线路。
        self.assertEqual(
            [
                item["normalized_url"]
                for item in result["accepted_routes"]
            ],
            [
                "https://example.com/new-fast.m3u8",
                "https://example.com/old-middle.m3u8",
            ],
        )
        self.assertEqual(result["channel_counts"]["CCTV-1"], 2)

    def test_domestic_verification_is_not_fabricated(self):
        result = self.process([make_candidate()])

        verification = result["accepted_routes"][0][
            "domestic_verification"
        ]

        self.assertEqual(
            verification["status"],
            "NOT_VERIFIED",
        )

    def test_subscription_outputs_are_consistent(self):
        result = self.process([
            make_candidate(),
            make_candidate(
                url="https://example.com/cctv5.m3u8",
                channel="CCTV-5",
            ),
        ])

        output = publisher.build_subscriptions(
            result["accepted_routes"],
            CHANNELS,
        )

        self.assertEqual(output["route_count"], 2)
        self.assertIn("#EXTM3U", output["m3u"])
        self.assertIn("CCTV-1,", output["txt"])
        self.assertIn("CCTV-5,", output["txt"])

        self.assertTrue(
            publisher.validate_subscriptions(
                output["m3u"],
                output["txt"],
                CHANNELS,
            )
        )

    def test_subscription_keeps_publisher_route_order(self):
        first = make_candidate(
            url="https://example.com/first.m3u8",
        )
        second = make_candidate(
            url="https://example.com/second.m3u8",
            priority=0,
        )

        for route in (first, second):
            route["selection"] = {"status": publisher.ACCEPTED}

        output = publisher.build_subscriptions(
            [first, second],
            CHANNELS,
        )

        self.assertLess(
            output["m3u"].index("https://example.com/first.m3u8"),
            output["m3u"].index("https://example.com/second.m3u8"),
        )

    def test_subscription_rejects_mismatched_content(self):
        m3u = (
            "#EXTM3U\n"
            "#EXTINF:-1,CCTV-1\n"
            "https://example.com/live.m3u8\n"
        )

        txt = "CCTV-5,https://example.com/live.m3u8\n"

        with self.assertRaises(publisher.PublisherError):
            publisher.validate_subscriptions(
                m3u,
                txt,
                CHANNELS,
            )

    def test_empty_subscription_generation_is_rejected(self):
        with self.assertRaises(publisher.PublisherError):
            publisher.build_subscriptions([], CHANNELS)

    def test_empty_subscription_content_is_rejected(self):
        with self.assertRaises(publisher.PublisherError):
            publisher.validate_subscriptions(
                "#EXTM3U\n",
                "",
                CHANNELS,
            )

    def test_invalid_channel_limit_is_rejected(self):
        invalid_settings = {
            "selection": {
                "max_routes_per_channel": 3,
            }
        }

        with self.assertRaises(publisher.PublisherError):
            publisher.process_batch(
                candidates=[],
                accepted_routes=[],
                channels=CHANNELS,
                settings=invalid_settings,
            )

    def test_invalid_url_is_rejected(self):
        result = self.process([
            make_candidate(url="not-a-valid-url")
        ])

        self.assertEqual(len(result["accepted_routes"]), 0)
        self.assertEqual(
            result["results"][0]["status"],
            publisher.REJECTED,
        )
        class MainPublisherHandoffIntegrationTests(unittest.TestCase):
    """验证 main 优选、Globalping 结果和真实 publisher 的完整交接。"""

    @staticmethod
    def _quality(
        segment_success,
        timeout_count,
        elapsed,
        read_seconds,
        bytes_read,
        request_success=3,
    ):
        return {
            "request_count": 3,
            "request_success_count": request_success,
            "timeout_count": timeout_count,
            "total_elapsed_seconds": elapsed,
            "total_read_seconds": read_seconds,
            "total_bytes_read": bytes_read,
            "segment_total": 3,
            "segment_success_count": segment_success,
        }

    @staticmethod
    def _local_result(candidate):
        return {
            **candidate,
            "media_type": "HLS",
            "checks": {
                "ipv4": {"status": "PASS"},
                "http": {"status": "PASS"},
                "hls": {"status": "PASS"},
                "ts": {"status": "PASS"},
            },
        }

    @staticmethod
    def _remote_result(candidate):
        return {
            **candidate,
            "globalping": {
                "status": "PASS",
                "checks": {
                    "ipv4": {"status": "PASS"},
                    "http": {"status": "PASS"},
                    "hls": {"status": "PASS"},
                    "ts": {"status": "PASS"},
                },
                "stages": {},
                "probes": {},
                "measurements": [],
                "error": None,
            },
        }

    def test_quality_order_and_publisher_quality_selection(self):
        """
        Globalping 按质量优先检测；
        main 恢复原始候选顺序；
        publisher 最终按真实质量选留线路。
        """
        auto_best = make_candidate(
            url="https://example.com/auto-best.m3u8",
            priority=1,
            source_order=1,
        )
        auto_best["quality"] = self._quality(
            segment_success=3,
            timeout_count=0,
            elapsed=1.0,
            read_seconds=0.5,
            bytes_read=3000,
        )

        manual_slow = make_candidate(
            url="https://example.com/manual-slow.m3u8",
            priority=0,
            source_order=0,
        )
        manual_slow["quality"] = self._quality(
            segment_success=1,
            timeout_count=1,
            elapsed=4.0,
            read_seconds=3.0,
            bytes_read=1000,
            request_success=2,
        )

        auto_first = make_candidate(
            url="https://example.com/auto-first.m3u8",
            priority=1,
            source_order=0,
        )
        auto_first["quality"] = self._quality(
            segment_success=2,
            timeout_count=0,
            elapsed=2.0,
            read_seconds=1.0,
            bytes_read=2000,
        )

        original_candidates = [
            auto_best,
            manual_slow,
            auto_first,
        ]

        remote_order = []

        def local_check(candidates, settings):
            return [
                self._local_result(candidate)
                for candidate in candidates
            ]

        def remote_check(candidates, settings):
            remote_order.extend(
                item["normalized_url"]
                for item in candidates
            )
            return [
                self._remote_result(candidate)
                for candidate in candidates
            ]

        detected = main.run_detection_pipeline(
            candidates=original_candidates,
            settings={
                "detection": {},
                "globalping": {
                    "max_probes": 3,
                },
            },
            local_check=local_check,
            remote_check=remote_check,
            channels=["CCTV-1"],
        )

        self.assertEqual(
            remote_order,
            [
                "https://example.com/auto-best.m3u8",
                "https://example.com/auto-first.m3u8",
                "https://example.com/manual-slow.m3u8",
            ],
        )

        self.assertEqual(
            [
                item["normalized_url"]
                for item in detected
            ],
            [
                "https://example.com/auto-best.m3u8",
                "https://example.com/manual-slow.m3u8",
                "https://example.com/auto-first.m3u8",
            ],
        )

        admission = main.process_admission_batch(
            candidates=detected,
            accepted_routes=[],
            channels=["CCTV-1"],
            settings=SETTINGS,
        )

        accepted_urls = [
            item["normalized_url"]
            for item in admission["accepted_routes"]
        ]

        self.assertEqual(
            accepted_urls,
            [
                "https://example.com/auto-best.m3u8",
                "https://example.com/auto-first.m3u8",
            ],
        )

        self.assertEqual(admission["channel_counts"]["CCTV-1"], 2)

        self.assertNotIn(
            "https://example.com/manual-slow.m3u8",
            accepted_urls,
        )

        accepted_auto = admission["accepted_routes"][0]

        self.assertEqual(
            accepted_auto["quality"],
            auto_best["quality"],
        )
        self.assertEqual(
            accepted_auto["globalping"]["status"],
            "PASS",
        )

        self.assertEqual(len(admission["results"]), 3)


if __name__ == "__main__":
    unittest.main()
