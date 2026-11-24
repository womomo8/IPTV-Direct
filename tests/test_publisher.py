
"""IPTV-Direct：publisher.py 线路准入与订阅生成专项测试。"""

import unittest

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
    width=1920,
    height=1080,
    fps=30,
    bitrate=4000000,
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
        "ts": {"status": "N/A"},
    }

    return {
        "channel": channel,
        "normalized_url": url,
        "media_type": media_type,
        "priority": priority,
        "source_order": source_order,
        "source_type": "manual" if priority == 0 else "automatic",
        "stream_quality": {
            "status": "PASS",
            "width": width,
            "height": height,
            "fps": fps,
            "bitrate": bitrate,
        },
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

    def test_stream_quality_must_explicitly_pass(self):
        """视频质量缺失、失败或未完成，都不得正式准入。"""

        for status in ("FAIL", "INCOMPLETE", None):
            candidate = make_candidate()

            if status is None:
                candidate.pop("stream_quality")
            else:
                candidate["stream_quality"]["status"] = status

            result = self.process([candidate])

            self.assertEqual(
                len(result["accepted_routes"]),
                0,
                f"视频质量状态 {status} 不应准入",
            )
            self.assertEqual(
                result["results"][0]["status"],
                publisher.REJECTED,
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

    def test_globalping_incomplete_is_rejected(self):
        """Globalping 未完成时，结果不充分，不允许准入。"""

        candidate = make_candidate(global_status="INCOMPLETE")

        result = self.process([candidate])

        self.assertEqual(len(result["accepted_routes"]), 0)
        self.assertEqual(
            result["results"][0]["status"],
            publisher.REJECTED,
        )
        self.assertIn(
            "INCOMPLETE",
            result["results"][0]["reason"],
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

    def test_channel_keeps_two_highest_quality_routes(self):
        """每频道最多两条，优先保留视频质量最高的线路。"""

        candidates = [
            make_candidate(
                url="https://example.com/low.m3u8",
                priority=0,
                source_order=0,
                width=1280,
                height=720,
                fps=30,
                bitrate=2000000,
            ),
            make_candidate(
                url="https://example.com/high.m3u8",
                priority=1,
                source_order=0,
                width=1920,
                height=1080,
                fps=30,
                bitrate=4000000,
            ),
            make_candidate(
                url="https://example.com/mid.m3u8",
                priority=0,
                source_order=1,
                width=1920,
                height=1080,
                fps=25,
                bitrate=3000000,
            ),
        ]

        result = self.process(candidates)

        self.assertEqual(len(result["accepted_routes"]), 2)
        self.assertEqual(result["channel_counts"]["CCTV-1"], 2)

        urls = [
            item["normalized_url"]
            for item in result["accepted_routes"]
        ]

        self.assertEqual(
            urls,
            [
                "https://example.com/high.m3u8",
                "https://example.com/mid.m3u8",
            ],
        )

    def test_quality_tie_does_not_use_source_priority(self):
        """质量相同时不得按来源优先级决定去留。"""

        automatic = make_candidate(
            url="https://example.com/automatic.m3u8",
            priority=1,
            source_order=0,
        )
        manual = make_candidate(
            url="https://example.com/manual.m3u8",
            priority=0,
            source_order=0,
        )

        automatic.pop("priority", None)
        automatic.pop("source_order", None)
        manual.pop("priority", None)
        manual.pop("source_order", None)

        result = self.process([manual, automatic])

        self.assertEqual(
            result["accepted_routes"][0]["normalized_url"],
            "https://example.com/automatic.m3u8",
        )

    def test_globalping_preselection_uses_quality_order(self):
        """Globalping 检测前必须按视频质量从高到低选择候选。"""

        low = make_candidate(
            url="https://example.com/low.m3u8",
            width=1280,
            height=720,
        )
        high = make_candidate(
            url="https://example.com/high.m3u8",
            width=3840,
            height=2160,
        )
        mid = make_candidate(
            url="https://example.com/mid.m3u8",
            width=1920,
            height=1080,
        )

        selected = publisher.next_candidate_for_detection(
            [low, high, mid],
            [],
            CHANNELS,
            SETTINGS,
        )

        self.assertEqual(
            selected["normalized_url"],
            high["normalized_url"],
        )

    def test_globalping_preselection_does_not_skip_weaker_full_channel(self):
        """即使频道已有两条线路，较弱候选仍必须完成检测。"""

        accepted = [
            make_candidate(
                url="https://example.com/cctv1-1.m3u8",
                channel="CCTV-1",
                source_order=0,
                width=1920,
                height=1080,
            ),
            make_candidate(
                url="https://example.com/cctv1-2.m3u8",
                channel="CCTV-1",
                source_order=1,
                width=1920,
                height=1080,
            ),
        ]

        weaker_full_channel = make_candidate(
            url="https://example.com/cctv1-weaker.m3u8",
            channel="CCTV-1",
            width=1280,
            height=720,
        )
        other_channel = make_candidate(
            url="https://example.com/cctv5.m3u8",
            channel="CCTV-5",
            width=1280,
            height=720,
        )

        selected = publisher.next_candidate_for_detection(
            [weaker_full_channel, other_channel],
            accepted,
            CHANNELS,
            SETTINGS,
        )

        self.assertEqual(
            selected["normalized_url"],
            weaker_full_channel["normalized_url"],
        )

    def test_globalping_preselection_continues_after_higher_quality_pass(self):
        """高质量候选通过后，较弱候选仍必须继续检测。"""

        historical = [
            make_candidate(
                url="https://example.com/history-1.m3u8",
                width=1280,
                height=720,
            ),
            make_candidate(
                url="https://example.com/history-2.m3u8",
                width=1920,
                height=1080,
                source_order=1,
            ),
        ]

        higher = make_candidate(
            url="https://example.com/new-higher.m3u8",
            width=3840,
            height=2160,
        )
        lower = make_candidate(
            url="https://example.com/new-lower.m3u8",
            width=640,
            height=480,
        )

        selected = publisher.next_candidate_for_detection(
            [lower, higher],
            historical,
            CHANNELS,
            SETTINGS,
        )

        self.assertEqual(
            selected["normalized_url"],
            higher["normalized_url"],
        )

        selected_after_pass = publisher.next_candidate_for_detection(
            [lower],
            historical + [higher],
            CHANNELS,
            SETTINGS,
        )

        self.assertEqual(
            selected_after_pass["normalized_url"],
            lower["normalized_url"],
        )
    
    def test_higher_quality_candidate_replaces_weaker_historical_route(self):
        """质量更高且检测合格的新线路可以替换较弱历史线路。"""

        historical = [
            make_candidate(
                url="https://example.com/old-1.m3u8",
                width=1280,
                height=720,
            ),
            make_candidate(
                url="https://example.com/old-2.m3u8",
                width=1280,
                height=720,
                source_order=1,
            ),
        ]

        old_result = self.process(historical)

        new_candidate = make_candidate(
            url="https://example.com/new-best.m3u8",
            width=3840,
            height=2160,
        )

        result = publisher.process_batch(
            candidates=[new_candidate],
            accepted_routes=old_result["accepted_routes"],
            channels=CHANNELS,
            settings=SETTINGS,
        )

        urls = {
            item["normalized_url"]
            for item in result["accepted_routes"]
        }

        self.assertEqual(len(result["accepted_routes"]), 2)
        self.assertIn(
            "https://example.com/new-best.m3u8",
            urls,
        )
        self.assertEqual(
            result["results"][0]["status"],
            publisher.ACCEPTED,
        )

    def test_historical_routes_compete_by_quality_and_keep_only_two(self):
        """超过两条的有效历史线路也必须统一排名，只保留质量最高两条。"""

        historical = [
            make_candidate(
                url="https://example.com/history-low.m3u8",
                width=1280,
                height=720,
            ),
            make_candidate(
                url="https://example.com/history-high.m3u8",
                width=3840,
                height=2160,
                source_order=1,
            ),
            make_candidate(
                url="https://example.com/history-mid.m3u8",
                width=1920,
                height=1080,
                source_order=2,
            ),
        ]

        result = publisher.process_batch(
            candidates=[],
            accepted_routes=historical,
            channels=CHANNELS,
            settings=SETTINGS,
        )

        urls = [
            item["normalized_url"]
            for item in result["accepted_routes"]
        ]

        self.assertEqual(
            urls,
            [
                "https://example.com/history-high.m3u8",
                "https://example.com/history-mid.m3u8",
            ],
        )
        self.assertEqual(result["channel_counts"]["CCTV-1"], 2)
        self.assertEqual(result["results"], [])

    def test_invalid_historical_routes_raise_error(self):
        """历史线路对象、频道、URL 或重复数据无效时必须报错。"""

        valid = make_candidate()

        invalid_cases = [
            [None],
            [{**valid, "channel": "UNKNOWN"}],
            [{**valid, "normalized_url": "invalid-url"}],
            [valid, dict(valid)],
        ]

        for historical in invalid_cases:
            with self.subTest(historical=historical):
                with self.assertRaises(publisher.PublisherError):
                    publisher.process_batch(
                        candidates=[],
                        accepted_routes=historical,
                        channels=CHANNELS,
                        settings=SETTINGS,
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

    def test_subscription_output_uses_quality_order(self):
        """最终订阅必须按视频质量排序，不受输入线路顺序影响。"""

        low = make_candidate(
            url="https://example.com/low.m3u8",
            width=1280,
            height=720,
        )
        high = make_candidate(
            url="https://example.com/high.m3u8",
            width=3840,
            height=2160,
        )

        result = self.process([low, high])

        output = publisher.build_subscriptions(
            list(reversed(result["accepted_routes"])),
            CHANNELS,
        )

        txt_entries = output["txt"].splitlines()

        self.assertEqual(
            txt_entries,
            [
                "CCTV-1,https://example.com/high.m3u8",
                "CCTV-1,https://example.com/low.m3u8",
            ],
        )

        m3u_entries = [
            output["m3u"].splitlines()[index + 1]
            for index, line in enumerate(output["m3u"].splitlines())
            if line.startswith("#EXTINF:")
        ]

        self.assertEqual(
            m3u_entries,
            [
                "https://example.com/high.m3u8",
                "https://example.com/low.m3u8",
            ],
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


if __name__ == "__main__":
    unittest.main()
