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
    }


class PublisherAdmissionTests(unittest.TestCase):

    def process(self, candidates):
        return publisher.process_batch(
            candidates=candidates,
            accepted_routes=[],
            channels=CHANNELS,
            settings=SETTINGS,
        )

    def test_hls_all_checks_pass_is_accepted(self):
        """HLS 本地适用检测全部通过，Globalping 通过，应准入。"""

        result = self.process([
            make_candidate()
        ])

        self.assertEqual(len(result["accepted_routes"]), 1)
        self.assertEqual(
            result["results"][0]["status"],
            publisher.ACCEPTED,
        )

    def test_direct_ts_with_hls_na_is_accepted(self):
        """直接 TS 的 HLS 必须为 N/A，其余适用检测通过。"""

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
        """本地任一适用检测失败，不得准入。"""

        candidate = make_candidate(local_status="FAIL")

        result = self.process([candidate])

        self.assertEqual(len(result["accepted_routes"]), 0)
        self.assertEqual(
            result["results"][0]["status"],
            publisher.REJECTED,
        )

    def test_globalping_failure_is_rejected(self):
        """Globalping 明确失败，不得准入。"""

        candidate = make_candidate(global_status="FAIL")

        result = self.process([candidate])

        self.assertEqual(len(result["accepted_routes"]), 0)
        self.assertEqual(
            result["results"][0]["status"],
            publisher.REJECTED,
        )

    def test_globalping_incomplete_can_be_accepted(self):
        """Globalping 未完成时，符合降级规则的线路可以准入。"""

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

    def test_unknown_media_type_is_rejected(self):
        """未知媒体类型不得进入正式订阅。"""

        candidate = make_candidate()
        candidate["media_type"] = "UNKNOWN"

        result = self.process([candidate])

        self.assertEqual(len(result["accepted_routes"]), 0)
        self.assertEqual(
            result["results"][0]["status"],
            publisher.REJECTED,
        )

    def test_duplicate_url_is_not_accepted_twice(self):
        """同一 URL 不得重复准入。"""

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

    def test_channel_keeps_at_most_two_routes(self):
        """每个频道最多保留两条线路。"""

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
        self.assertEqual(result["accepted_count"], 2)
        self.assertEqual(result["rejected_count"], 1)

        urls = [
            item["normalized_url"]
            for item in result["accepted_routes"]
        ]

        self.assertEqual(
            urls,
            [
                "https://example.com/manual-1.m3u8",
                "https://example.com/manual-2.m3u8",
            ],
        )

    def test_manual_source_has_priority(self):
        """人工来源必须优先于自动来源。"""

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
            result["accepted_routes"][0]["normalized_url"],
            "https://example.com/manual.m3u8",
        )

    def test_domestic_verification_is_not_fabricated(self):
        """没有国内实测证据时，必须标记 NOT_VERIFIED。"""

        result = self.process([make_candidate()])

        verification = result["accepted_routes"][0][
            "domestic_verification"
        ]

        self.assertEqual(
            verification["status"],
            "NOT_VERIFIED",
        )

    def test_subscription_outputs_are_consistent(self):
        """M3U 和 TXT 必须来自同一份准入线路数据。"""

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

    def test_subscription_rejects_mismatched_content(self):
        """M3U 与 TXT 内容不一致时必须报错。"""

        m3u = (
            "#EXTM3U\n"
            "#EXTINF:-1,CCTV-1\n"
            "https://example.com/live.m3u8\n"
        )

        txt = (
            "CCTV-5,https://example.com/live.m3u8\n"
        )

        with self.assertRaises(publisher.PublisherError):
            publisher.validate_subscriptions(
                m3u,
                txt,
                CHANNELS,
            )

    def test_empty_subscription_generation_is_rejected(self):
        """没有任何准入线路时，不得生成空订阅。"""

        with self.assertRaises(publisher.PublisherError):
            publisher.build_subscriptions([], CHANNELS)

    def test_empty_subscription_content_is_rejected(self):
        """M3U只有文件头、TXT为空时，必须拒绝。"""

        with self.assertRaises(publisher.PublisherError):
            publisher.validate_subscriptions(
                "#EXTM3U\n",
                "",
                CHANNELS,
            )

    def test_invalid_channel_limit_is_rejected(self):
        """每频道线路上限不得偏离正式标准 2。"""

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
        """无效播放地址不得准入。"""

        candidate = make_candidate(
            url="not-a-valid-url"
        )

        result = self.process([candidate])

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

    def test_quality_order_and_publisher_priority_are_both_preserved(self):
        """
        Globalping 按质量优先检测；
        main 恢复原始候选顺序；
        publisher 仍按人工来源和来源顺序准入。
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

        # 第一层：Globalping 收到的是同频道质量优先顺序。
        self.assertEqual(
            remote_order,
            [
                "https://example.com/auto-best.m3u8",
                "https://example.com/auto-first.m3u8",
                "https://example.com/manual-slow.m3u8",
            ],
        )

        # 第二层：main 返回时恢复候选池原始顺序。
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

        # 第三层：真实 publisher 执行人工优先、来源顺序和数量上限。
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
                "https://example.com/manual-slow.m3u8",
                "https://example.com/auto-first.m3u8",
            ],
        )

        self.assertEqual(admission["channel_counts"]["CCTV-1"], 2)

        # 自动来源中，source_order=0 的线路优先于 source_order=1。
        # 质量最好的 auto_best 不得绕过已确定的来源顺序政策。
        self.assertNotIn(
            "https://example.com/auto-best.m3u8",
            accepted_urls,
        )

        # 本地质量数据和 Globalping 结果必须完整保留到正式准入结果。
        accepted_auto = admission["accepted_routes"][1]

        self.assertEqual(
            accepted_auto["quality"],
            auto_first["quality"],
        )
        self.assertEqual(
            accepted_auto["globalping"]["status"],
            "PASS",
        )

        self.assertEqual(
            len(admission["results"]),
            3,
        )


if __name__ == "__main__":
    unittest.main()
