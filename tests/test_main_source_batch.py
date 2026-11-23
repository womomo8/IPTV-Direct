
"""main.py 逐条来源批次调度专项测试。"""

import unittest
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
            "status": "NA",
            "reason": "直接 TS 不适用 HLS 检测",
        }

    return {
        **candidate,
        "media_type": media_type,
        "checks": checks,
    }

def make_remote(candidate, status="PASS", measured=True):
    checks = {
        name: {
            "status": status,
            "reason": "测试结果",
        }
        for name in ("ipv4", "http", "hls", "ts")
    }

    return {
        **make_local(candidate),
        "globalping": {
            "status": status,
            "checks": checks,
            "stages": {},
            "probes": {},
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

        def remote_side_effect(items, settings, session=None):
            self.assertEqual(len(items), 1)
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

    def test_channel_stops_after_two_accepted_routes(self):
        candidates = [
            make_candidate("频道一", 1, order=0),
            make_candidate("频道一", 2, order=1),
            make_candidate("频道一", 3, order=2),
        ]

        result, _, _, _, remote = self.run_batch(candidates)

        self.assertEqual(remote.call_count, 2)
        self.assertEqual(len(result[1]["accepted_routes"]), 2)
        self.assertEqual(
            sum(
                item["status"] == publisher.REJECTED
                for item in result[1]["results"]
            ),
            1,
        )

    def test_manual_source_precedes_automatic_source(self):
        automatic = make_candidate(
            "频道一", 1, priority=1, order=0
        )
        manual = make_candidate(
            "频道二", 2, priority=0, order=0
        )

        result, _, _, _, remote = self.run_batch(
            [automatic, manual]
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
            ],
        )

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
            publisher.ACCEPTED,
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

if __name__ == "__main__":
    unittest.main()
