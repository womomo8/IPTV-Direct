
"""main.py 全局候选批次调度专项测试。"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import main
import publisher


SETTINGS = {
    "detection": {},
    "globalping": {"max_probes": 3},
    "selection": {"max_routes_per_channel": 2},
}

CHANNELS = ["频道一", "频道二"]


def make_candidate(channel, number, priority=0, order=0):
    return {
        "channel": channel,
        "normalized_url": f"https://example.test/{number}.m3u8",
        "priority": priority,
        "source_order": order,
    }

def make_local(candidate, media_type="HLS"):
    checks = {
        name: {"status": "PASS", "reason": "测试通过"}
        for name in ("ipv4", "http", "hls", "ts")
    }
    if media_type == "DIRECT_TS":
        checks["hls"] = {
            "status": "N/A",
            "reason": "直接 TS 不适用 HLS 检测",
        }

    return {
        **candidate,
        "media_type": media_type,
        "checks": checks,
    }


def make_remote(candidate, status="PASS", measured=True):
    media_type = candidate.get("media_type", "HLS")
    checks = {
        name: {
            "status": status,
            "reason": "测试结果",
        }
        for name in ("ipv4", "http")
    }

    if media_type == "DIRECT_TS":
        checks["hls"] = {
            "status": "N/A",
            "reason": "直接 TS 不适用 HLS 检测",
        }
    else:
        checks["hls"] = {
            "status": status,
            "reason": "测试结果",
        }

    checks["ts"] = {
        "status": "N/A",
        "reason": "TS 内容由本地 checker.py 检测",
    }

    stages = {}
    probes = {}

    for index, asn in enumerate((10001, 10002, 10003), start=1):
        probe = {
            "country": "CN",
            "city": "Test",
            "network": "Test Eyeball Network",
            "asn": asn,
        }
        probes[f"probe_{index}"] = probe
        stages[f"probe_{index}"] = {
            "asn": asn,
            "probe": probe,
            "measurement_id": f"mock-measurement-{index}",
            "status": status,
            "checks": {
                name: dict(item)
                for name, item in checks.items()
            },
        }

    return {
        **make_local(candidate),
        "globalping": {
            "status": status,
            "checks": checks,
            "stages": stages,
            "probes": probes,
            "measurements": (
                ["mock-measurement"] if measured else []
            ),
            "error": None if status == "PASS" else "模拟未完成",
        },
    }


class SourceBatchPipelineTests(unittest.TestCase):

    def setUp(self):
        self.session = {
            "locations": [{"limit": 1}],
            "budget": {
                "remaining": 10,
                "probe_count": 1,
                "halted": False,
            },
            "error": None,
        }

    def run_batch(
        self,
        candidates,
        statuses=None,
        stream_status="PASS",
        stream_error=None,
        local_failure=False,
        remote_error=None,
        remote_transform=None,
        local_media_type="HLS",
    ):
        local_results = [
            make_local(item, local_media_type)
            for item in candidates
        ]
        if local_failure and local_results:
            local_results[0]["checks"]["http"] = {
                "status": "FAIL",
                "reason": "模拟本地 HTTP 失败",
            }
        statuses = statuses or ["PASS"] * len(candidates)

        process_batch_mock = None
        
        def remote_side_effect(items, settings, session=None):
            self.assertEqual(len(items), 1)
            self.assertEqual(process_batch_mock.call_count, 0)
            candidate = items[0]
            if remote_error is not None:
                raise RuntimeError(remote_error)
            status = statuses_by_url[candidate["normalized_url"]]
            result = make_remote(candidate, status)
            if remote_transform is not None:
                result = remote_transform(result)
            return [result]

        statuses_by_url = {
            item["normalized_url"]: statuses[index]
            for index, item in enumerate(candidates)
        }

        stream_results = [
            {
                **item,
                "stream_quality": {"status": stream_status},
            }
            for item in local_results
        ]

        with (
            patch.object(
                main.checker,
                "check_candidates",
                return_value=local_results,
            ) as local,
            patch.object(
                main.stream,
                "check_candidates",
                return_value=stream_results,
                side_effect=stream_error,
            ) as stream_check,
            patch.object(
                main.globalping,
                "create_session",
                return_value=self.session,
            ) as create_session,
            patch.object(
                publisher,
                "process_batch",
                wraps=publisher.process_batch,
            ) as process_batch_mock,
            patch.object(
                main.globalping,
                "check_candidates",
                side_effect=remote_side_effect,
            ) as remote,
        ):
            result = main.run_source_batch_pipeline(
                candidates,
                [],
                SETTINGS,
                CHANNELS,
            )
            process_batch_mock.assert_called_once()

        return (
            result,
            local,
            stream_check,
            create_session,
            remote,
        )

    def test_only_stream_pass_can_enter_globalping(self):
        """FAIL、INCOMPLETE 和无效状态均不得进入 Globalping。"""

        for status in ("FAIL", "INCOMPLETE", None, "UNKNOWN"):
            with self.subTest(stream_status=status):
                candidates = [
                    make_candidate("频道一", 1),
                ]

                (
                    result,
                    _,
                    _,
                    create_session,
                    remote,
                ) = self.run_batch(
                    candidates,
                    stream_status=status,
                )

                self.assertEqual(
                    result[0][0]["stream_quality"]["status"],
                    status,
                )
                self.assertEqual(
                    len(result[1]["accepted_routes"]),
                    0,
                )
                create_session.assert_not_called()
                remote.assert_not_called()

    def test_stream_exception_does_not_call_globalping(self):
        """视频质量检测异常必须按 INCOMPLETE 处理。"""

        candidates = [
            make_candidate("频道一", 1),
        ]

        (
            result,
            _,
            _,
            create_session,
            remote,
        ) = self.run_batch(
            candidates,
            stream_error=RuntimeError("模拟视频检测异常"),
        )

        self.assertEqual(
            result[0][0]["stream_quality"]["status"],
            "INCOMPLETE",
        )
        self.assertEqual(
            len(result[1]["accepted_routes"]),
            0,
        )
        create_session.assert_not_called()
        remote.assert_not_called()

    def test_local_check_and_session_are_each_created_once(self):
        candidates = [
            make_candidate("频道一", 1),
            make_candidate("频道二", 2),
        ]

        result, local, stream_check, create_session, remote = (
            self.run_batch(candidates)
        )

        local.assert_called_once()
        stream_check.assert_called_once()
        create_session.assert_called_once()
        self.assertEqual(remote.call_count, 2)
        self.assertEqual(len(result[0]), 2)
        self.assertEqual(len(result[1]["accepted_routes"]), 2)

    def test_all_candidates_enter_globalping_before_final_selection(self):
        candidates = [
            make_candidate("频道一", 1, order=0),
            make_candidate("频道一", 2, order=1),
            make_candidate("频道一", 3, order=2),
        ]

        result, _, _, _, remote = self.run_batch(candidates)

        self.assertEqual(remote.call_count, 3)
        self.assertEqual(len(result[0]), 3)
        self.assertEqual(len(result[1]["accepted_routes"]), 2)

    def test_failed_remote_candidates_do_not_consume_channel_slots(self):
        candidates = [
            make_candidate("频道一", 1, order=0),
            make_candidate("频道一", 2, order=1),
            make_candidate("频道一", 3, order=2),
            make_candidate("频道一", 4, order=3),
            make_candidate("频道一", 5, order=4),
        ]

        result, _, _, _, remote = self.run_batch(
            candidates,
            ["FAIL", "INCOMPLETE", "PASS", "PASS", "PASS"],
        )

        self.assertEqual(remote.call_count, 5)
        self.assertEqual(len(result[1]["accepted_routes"]), 2)
        self.assertEqual(
            [
                item["globalping"]["status"]
                for item in result[0]
            ],
            [
                "FAIL",
                "INCOMPLETE",
                "PASS",
                "PASS",
                                "PASS",
            ],
        )
    
    def test_manual_source_precedes_automatic_source(self):
        automatic = make_candidate(
            "频道一", 1, priority=1, order=0
        )
        manual = make_candidate(
            "频道二", 2, priority=0, order=0
        )
        third = make_candidate(
            "频道一", 3, priority=1, order=1
        )

        result, _, _, _, remote = self.run_batch(
            [automatic, manual, third],
            ["PASS", "FAIL", "INCOMPLETE"],
        )

        first_remote_candidate = remote.call_args_list[0].args[0][0]

        self.assertEqual(
            first_remote_candidate["normalized_url"],
            manual["normalized_url"],
        )
        self.assertEqual(
            [item["normalized_url"] for item in result[0]],
            [
                automatic["normalized_url"],
                manual["normalized_url"],
                third["normalized_url"],
            ],
        )
        self.assertEqual(
            [
                item["globalping"]["status"]
                for item in result[0]
            ],
            ["PASS", "FAIL", "INCOMPLETE"],
        )
        self.assertEqual(remote.call_count, 3)

    def test_hls_pass_is_preserved(self):
        candidate = make_candidate("频道一", 1)

        def add_details(result):
            result["globalping"]["stages"] = {
                "test_stage": {"status": "PASS"}
            }
            result["globalping"]["probes"] = {
                "test_probe": {"country": "CN"}
            }
            return result

        result, _, _, _, remote = self.run_batch(
            [candidate],
            remote_transform=add_details,
        )

        remote_result = result[0][0]["globalping"]
        self.assertEqual(remote_result["status"], "PASS")
        self.assertEqual(remote_result["checks"]["hls"]["status"], "PASS")
        self.assertEqual(
            remote_result["stages"]["test_stage"]["status"],
            "PASS",
        )
        self.assertEqual(
            remote_result["probes"]["test_probe"]["country"],
            "CN",
        )
        remote.assert_called_once()

    def test_hls_fail_is_preserved(self):
        candidate = make_candidate("频道一", 1)

        def add_failure(result):
            result["globalping"]["checks"]["http"] = {
                "status": "FAIL",
                "reason": "HTTP 检测明确失败",
            }
            return result

        result, _, _, _, remote = self.run_batch(
            [candidate],
            statuses=["FAIL"],
            remote_transform=add_failure,
        )

        remote_result = result[0][0]["globalping"]
        self.assertEqual(remote_result["status"], "FAIL")
        self.assertEqual(
            remote_result["checks"]["http"]["status"],
            "FAIL",
        )
        self.assertIn(
            "HTTP 检测明确失败",
            remote_result["checks"]["http"]["reason"],
        )
        remote.assert_called_once()

    def test_hls_incomplete_is_preserved(self):
        candidate = make_candidate("频道一", 1)

        result, _, _, _, remote = self.run_batch(
            [candidate],
            statuses=["INCOMPLETE"],
        )

        remote_result = result[0][0]["globalping"]
        self.assertEqual(remote_result["status"], "INCOMPLETE")
        self.assertEqual(
            remote_result["checks"]["ts"]["status"],
            "N/A",
        )
        remote.assert_called_once()

    def test_direct_ts_preserves_hls_na(self):
        candidate = make_candidate("频道一", 1)

        def make_direct_ts(result):
            result["media_type"] = "DIRECT_TS"
            result["checks"]["hls"] = {
                "status": "N/A",
                "reason": "直接 TS 不适用 HLS 检测",
            }
            result["globalping"]["checks"]["hls"] = {
                "status": "N/A",
                "reason": "直接 TS 不适用 HLS 检测",
            }
            return result

        result, _, _, _, remote = self.run_batch(
            [candidate],
            local_media_type="DIRECT_TS",
            remote_transform=make_direct_ts,
        )

        item = result[0][0]
        self.assertEqual(item["globalping"]["status"], "PASS")
        self.assertEqual(item["checks"]["hls"]["status"], "N/A")
        self.assertEqual(
            item["globalping"]["checks"]["hls"]["status"],
            "N/A",
        )
        self.assertEqual(
            item["globalping"]["checks"]["ts"]["status"],
            "N/A",
        )
        remote.assert_called_once()   
    
    def test_globalping_fail_rejected_and_incomplete_uses_downgrade(self):
        failed = make_candidate("频道一", 1, order=0)
        incomplete = make_candidate("频道二", 2, order=1)

        result, _, _, _, _ = self.run_batch(
            [failed, incomplete],
            ["FAIL", "INCOMPLETE"],
        )

        self.assertEqual(
            result[0][0]["globalping"]["status"],
            "FAIL",
        )
        self.assertEqual(
            result[0][1]["globalping"]["status"],
            "INCOMPLETE",
        )
        self.assertEqual(
            result[1]["results"][0]["status"],
            publisher.REJECTED,
        )
        self.assertEqual(
            result[1]["results"][1]["status"],
            publisher.REJECTED,
        )

    def test_local_failure_does_not_call_globalping(self):
        candidate = make_candidate("频道一", 1)
        result, local, stream_check, create_session, remote = (
            self.run_batch([candidate], local_failure=True)
        )

        self.assertEqual(result[0][0]["checks"]["http"]["status"], "FAIL")
        self.assertEqual(result[0][0]["globalping"]["status"], "INCOMPLETE")
        local.assert_called_once()
        stream_check.assert_not_called()
        create_session.assert_not_called()
        remote.assert_not_called()

    def test_globalping_exception_becomes_incomplete(self):
        candidate = make_candidate("频道一", 1)
        result, _, _, create_session, remote = self.run_batch(
            [candidate],
            remote_error="模拟 Globalping API 异常",
        )

        self.assertEqual(result[0][0]["globalping"]["status"], "INCOMPLETE")
        self.assertIn("模拟 Globalping API 异常", result[0][0]["globalping"]["error"])
        create_session.assert_called_once()
        remote.assert_called_once()

    def test_wrong_remote_identity_becomes_incomplete(self):
        candidate = make_candidate("频道一", 1)

        def wrong_identity(_result):
            wrong = make_candidate("频道二", 99)
            return make_remote(wrong)

        result, _, _, _, remote = self.run_batch(
            [candidate],
            remote_transform=wrong_identity,
        )

        self.assertEqual(result[0][0]["globalping"]["status"], "INCOMPLETE")
        self.assertIn("身份不匹配", result[0][0]["globalping"]["error"])
        remote.assert_called_once()

    def test_invalid_remote_status_becomes_incomplete(self):
        candidate = make_candidate("频道一", 1)

        def invalid_status(remote_result):
            remote_result["globalping"]["status"] = "UNKNOWN"
            return remote_result

        result, _, _, _, remote = self.run_batch(
            [candidate],
            remote_transform=invalid_status,
        )

        self.assertEqual(result[0][0]["globalping"]["status"], "INCOMPLETE")
        self.assertIn("有效总体状态", result[0][0]["globalping"]["error"])
        remote.assert_called_once()

    def test_non_target_channel_does_not_call_globalping(self):
        candidate = make_candidate("非目标频道", 1)
        result, local, stream_check, create_session, remote = (
            self.run_batch([candidate])
        )

        self.assertEqual(result[0][0]["globalping"]["status"], "INCOMPLETE")
        self.assertIn(
            "不在 channels.txt 目标频道列表中",
            result[0][0]["globalping"]["error"],
        )
        local.assert_called_once()
        stream_check.assert_not_called()
        create_session.assert_not_called()
        remote.assert_not_called()

class HistoricalCandidateTests(unittest.TestCase):

    def setUp(self):
        self.settings = {
            "collection": {
                "max_valid_sources": 10,
                "max_candidates_per_source": 10,
                "max_candidates_total": 10,
                "max_index_links_per_page": 10,
            },
            "detection": {
                "max_hls_manifest_bytes": 1024,
            },
            "globalping": {
                "max_probes": 3,
            },
            "selection": {
                "max_routes_per_channel": 2,
            },
            "aliases": {},
        }

    def write_history(self, root):
        output = root / "output"
        output.mkdir(parents=True, exist_ok=True)

        (root / "channels.txt").write_text(
            "频道一\n",
            encoding="utf-8",
        )

        m3u = (
            "#EXTM3U\n"
            '#EXTINF:-1 tvg-name="频道一",频道一\n'
            "https://example.test/history.m3u8\n"
        )

        txt = (
            "频道一,https://example.test/history.m3u8\n"
        )

        (output / "iptv.m3u").write_text(
            m3u,
            encoding="utf-8",
        )
        (output / "iptv.txt").write_text(
            txt,
            encoding="utf-8",
        )

    def test_first_publish_without_history_returns_empty(self):
        with TemporaryDirectory() as directory:
            candidates = main._load_historical_candidates(
                Path(directory),
                ["频道一"],
                self.settings,
            )

        self.assertEqual(candidates, [])

    def test_valid_history_enters_candidate_pool(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_history(root)

            candidates = main._load_historical_candidates(
                root,
                ["频道一"],
                self.settings,
            )

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["channel"], "频道一")
        self.assertEqual(
            candidates[0]["normalized_url"],
            "https://example.test/history.m3u8",
        )
        self.assertEqual(candidates[0]["source_type"], "history")
        self.assertEqual(candidates[0]["priority"], 1)
        self.assertEqual(
            candidates[0]["source_order"],
            self.settings["collection"]["max_candidates_total"],
        )

    def test_missing_one_history_file_fails_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_history(root)
            (root / "output" / "iptv.txt").unlink()

            with self.assertRaises(publisher.PublisherError):
                main._load_historical_candidates(
                    root,
                    ["频道一"],
                    self.settings,
                )

    def test_mismatched_history_files_fail_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_history(root)

            (root / "output" / "iptv.txt").write_text(
                "频道一,https://example.test/different.m3u8\n",
                encoding="utf-8",
            )

            with self.assertRaises(publisher.PublisherError):
                main._load_historical_candidates(
                    root,
                    ["频道一"],
                    self.settings,
                )

    def test_formal_flow_sends_history_to_detection_pipeline(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_history(root)

            history = main._load_historical_candidates(
                root,
                ["频道一"],
                self.settings,
            )

            detected = [{
                **history[0],
                "media_type": "HLS",
                "checks": {},
                "stream_quality": {
                    "status": "INCOMPLETE",
                    "reason": "测试模拟",
                },
                "globalping": {
                    "status": "INCOMPLETE",
                    "error": "测试模拟",
                },
            }]

            admission = {
                "accepted_routes": [],
                "channel_counts": {"频道一": 0},
                "results": [{
                    "candidate": history[0],
                    "status": publisher.REJECTED,
                    "reason": "测试模拟",
                }],
            }

            config = {
                "settings": self.settings,
                "channels": ["频道一"],
                "manual_sources": [],
                "automatic_sources": [],
            }

            publish_result = {
                "channel_count": 0,
                "route_count": 0,
                "m3u_path": "output/iptv.m3u",
                "txt_path": "output/iptv.txt",
                "first_publish": False,
            }

            with (
                patch.object(main, "ROOT", root),
                patch.object(
                    main,
                    "iter_source_contents",
                    return_value=iter(()),
                ),
                patch.object(
                    main,
                    "run_source_batch_pipeline",
                    return_value=(detected, admission, []),
                ) as pipeline,
                patch.object(
                    publisher,
                    "build_subscriptions",
                    return_value={
                        "route_count": 0,
                        "channel_counts": {"频道一": 0},
                    },
                ),
                patch.object(
                    publisher,
                    "publish_subscriptions",
                    return_value=publish_result,
                ),
            ):
                result = main.run_formal_flow(config)

            self.assertEqual(result, 0)

            submitted = pipeline.call_args.kwargs["candidates"]

            self.assertEqual(len(submitted), 1)
            self.assertEqual(
                submitted[0]["normalized_url"],
                "https://example.test/history.m3u8",
            )

if __name__ == "__main__":
    unittest.main()
