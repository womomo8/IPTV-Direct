
"""main.py 与 Globalping 的主程序交接测试。

全部使用模拟检测函数，不访问真实线路，不调用 Globalping API，
不创建真实测量，不消耗额度，不修改正式订阅。
"""

import unittest
from unittest.mock import Mock

import main


PASS = "PASS"
FAIL = "FAIL"
INCOMPLETE = "INCOMPLETE"
NA = "N/A"


SETTINGS = {
    "detection": {},
    "globalping": {
        "max_probes": 3,
    },
}

TARGET_CHANNELS = [
    "测试频道",
    "直接 TS 测试频道",
    "频道一",
    "频道二",
    "频道三",
]


def make_candidate(name="测试频道", url="https://example.test/live.m3u8"):
    return {
        "channel": name,
        "normalized_url": url,
    }


def make_local_result(candidate, media_type="HLS", status=PASS):
    hls_status = NA if media_type == "DIRECT_TS" else status

    return {
        **candidate,
        "media_type": media_type,
        "checks": {
            "ipv4": {"status": status, "reason": "local test"},
            "http": {"status": status, "reason": "local test"},
            "hls": {"status": hls_status, "reason": "local test"},
            "ts": {"status": status, "reason": "local test"},
        },
    }


def make_remote_result(candidate, status=PASS, media_type="HLS"):
    hls_status = NA if media_type == "DIRECT_TS" else status

    return {
        **make_local_result(candidate, media_type, PASS),
        "globalping": {
            "status": status,
            "checks": {
                "ipv4": {"status": status, "reason": "remote test"},
                "http": {"status": status, "reason": "remote test"},
                "hls": {"status": hls_status, "reason": "remote test"},
                "ts": {"status": status, "reason": "remote test"},
            },
            "stages": {
                "test_stage": {
                    "status": status,
                    "reason": "stage test",
                }
            },
            "probes": {
                "test_probe": {
                    "country": "CN",
                    "network": "China Mobile",
                }
            },
            "measurements": ["mock-measurement"],
            "error": None if status == PASS else "模拟状态",
        },
    }


class MainGlobalpingIntegrationTests(unittest.TestCase):

    def run_pipeline(self, candidate, local_result, remote_result):
        local = Mock(return_value=[local_result])
        remote = Mock(return_value=[remote_result])

        result = main.run_detection_pipeline(
            [candidate],
            SETTINGS,
            TARGET_CHANNELS,
            local,
            remote,
        )

        return result, local, remote

    def test_hls_pass_is_preserved(self):
        candidate = make_candidate()

        result, local, remote = self.run_pipeline(
            candidate,
            make_local_result(candidate, "HLS", PASS),
            make_remote_result(candidate, PASS, "HLS"),
        )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["globalping"]["status"], PASS)
        self.assertEqual(
            result[0]["globalping"]["checks"]["hls"]["status"],
            PASS,
        )
        self.assertEqual(
            result[0]["globalping"]["stages"]["test_stage"]["status"],
            PASS,
        )
        self.assertEqual(
            result[0]["globalping"]["probes"]["test_probe"]["country"],
            "CN",
        )
        local.assert_called_once()
        remote.assert_called_once()

    def test_hls_fail_is_preserved(self):
        candidate = make_candidate()

        remote_result = make_remote_result(candidate, FAIL, "HLS")
        remote_result["globalping"]["checks"]["http"] = {
            "status": FAIL,
            "reason": "HTTP 检测明确失败",
        }

        result, _, remote = self.run_pipeline(
            candidate,
            make_local_result(candidate, "HLS", PASS),
            remote_result,
        )

        self.assertEqual(result[0]["globalping"]["status"], FAIL)
        self.assertEqual(
            result[0]["globalping"]["checks"]["http"]["status"],
            FAIL,
        )
        self.assertIn(
            "HTTP 检测明确失败",
            result[0]["globalping"]["checks"]["http"]["reason"],
        )
        remote.assert_called_once()

    def test_hls_incomplete_is_preserved(self):
        candidate = make_candidate()

        remote_result = make_remote_result(
            candidate,
            INCOMPLETE,
            "HLS",
        )

        result, _, remote = self.run_pipeline(
            candidate,
            make_local_result(candidate, "HLS", PASS),
            remote_result,
        )

        self.assertEqual(
            result[0]["globalping"]["status"],
            INCOMPLETE,
        )
        self.assertEqual(
            result[0]["globalping"]["checks"]["ts"]["status"],
            INCOMPLETE,
        )
        remote.assert_called_once()

    def test_direct_ts_preserves_hls_na(self):
        candidate = make_candidate(
            "直接 TS 测试频道",
            "https://example.test/live.ts",
        )

        result, _, remote = self.run_pipeline(
            candidate,
            make_local_result(candidate, "DIRECT_TS", PASS),
            make_remote_result(candidate, PASS, "DIRECT_TS"),
        )

        self.assertEqual(
            result[0]["globalping"]["status"],
            PASS,
        )
        self.assertEqual(
            result[0]["checks"]["hls"]["status"],
            NA,
        )
        self.assertEqual(
            result[0]["globalping"]["checks"]["hls"]["status"],
            NA,
        )
        self.assertEqual(
            result[0]["globalping"]["checks"]["ts"]["status"],
            PASS,
        )
        remote.assert_called_once()

    def test_local_failure_does_not_call_globalping(self):
        candidate = make_candidate()

        local_result = make_local_result(
            candidate,
            "HLS",
            PASS,
        )
        local_result["checks"]["http"] = {
            "status": FAIL,
            "reason": "本地 HTTP 失败",
        }

        local = Mock(return_value=[local_result])
        remote = Mock(
            side_effect=AssertionError(
                "本地未全部通过，不应调用 Globalping"
            )
        )

        result = main.run_detection_pipeline(
            [candidate],
            SETTINGS,
            TARGET_CHANNELS,
            local,
            remote,
        )

        self.assertEqual(len(result), 1)
        self.assertEqual(
            result[0]["globalping"]["status"],
            INCOMPLETE,
        )
        self.assertIn(
            "未提交 Globalping",
            result[0]["globalping"]["error"],
        )
        local.assert_called_once()
        remote.assert_not_called()

    def test_globalping_exception_becomes_incomplete(self):
        candidate = make_candidate()

        local = Mock(
            return_value=[
                make_local_result(candidate, "HLS", PASS)
            ]
        )
        remote = Mock(
            side_effect=RuntimeError("模拟 Globalping API 异常")
        )

        result = main.run_detection_pipeline(
            [candidate],
            SETTINGS,
            TARGET_CHANNELS,
            local,
            remote,
        )

        self.assertEqual(
            result[0]["globalping"]["status"],
            INCOMPLETE,
        )
        self.assertIn(
            "模拟 Globalping API 异常",
            result[0]["globalping"]["error"],
        )

    def test_wrong_remote_identity_becomes_incomplete(self):
        candidate = make_candidate()

        wrong_candidate = make_candidate(
            "错误频道",
            "https://example.test/wrong.m3u8",
        )

        local = Mock(
            return_value=[
                make_local_result(candidate, "HLS", PASS)
            ]
        )
        remote = Mock(
            return_value=[
                make_remote_result(wrong_candidate, PASS, "HLS")
            ]
        )

        result = main.run_detection_pipeline(
            [candidate],
            SETTINGS,
            TARGET_CHANNELS,
            local,
            remote,
        )

        self.assertEqual(
            result[0]["globalping"]["status"],
            INCOMPLETE,
        )
        self.assertIn(
            "身份不匹配",
            result[0]["globalping"]["error"],
        )

    def test_invalid_remote_status_becomes_incomplete(self):
        candidate = make_candidate()

        invalid_result = make_remote_result(
            candidate,
            PASS,
            "HLS",
        )
        invalid_result["globalping"]["status"] = "UNKNOWN"

        local = Mock(
            return_value=[
                make_local_result(candidate, "HLS", PASS)
            ]
        )
        remote = Mock(return_value=[invalid_result])

        result = main.run_detection_pipeline(
            [candidate],
            SETTINGS,
            TARGET_CHANNELS,
            local,
            remote,
        )

        self.assertEqual(
            result[0]["globalping"]["status"],
            INCOMPLETE,
        )

    def test_multiple_candidates_keep_original_order(self):
        first = make_candidate(
            "频道一",
            "https://example.test/one.m3u8",
        )
        second = make_candidate(
            "频道二",
            "https://example.test/two.m3u8",
        )
        third = make_candidate(
            "频道三",
            "https://example.test/three.m3u8",
        )

        candidates = [first, second, third]

        local = Mock(
            return_value=[
                make_local_result(first, "HLS", PASS),
                make_local_result(second, "HLS", PASS),
                make_local_result(third, "HLS", PASS),
            ]
        )

        remote = Mock(
            return_value=[
                make_remote_result(first, PASS, "HLS"),
                make_remote_result(second, FAIL, "HLS"),
                make_remote_result(third, INCOMPLETE, "HLS"),
            ]
        )

        result = main.run_detection_pipeline(
            candidates,
            SETTINGS,
            TARGET_CHANNELS,
            local,
            remote,
        )

        self.assertEqual(
            [item["channel"] for item in result],
            ["频道一", "频道二", "频道三"],
        )
        self.assertEqual(
            [item["globalping"]["status"] for item in result],
            [PASS, FAIL, INCOMPLETE],
        )

    def test_non_target_channel_does_not_call_globalping(self):
        candidate = make_candidate(
            "非目标频道",
            "https://example.test/non-target.m3u8",
        )

        local = Mock(
            return_value=[
                make_local_result(candidate, "HLS", PASS)
            ]
        )
        remote = Mock(
            side_effect=AssertionError(
                "非目标频道不得提交 Globalping"
            )
        )

        result = main.run_detection_pipeline(
            [candidate],
            SETTINGS,
            TARGET_CHANNELS,
            local,
            remote,
        )

        self.assertEqual(len(result), 1)
        self.assertEqual(
            result[0]["globalping"]["status"],
            INCOMPLETE,
        )
        self.assertIn(
            "不在 channels.txt 目标频道列表中",
            result[0]["globalping"]["error"],
        )
        local.assert_called_once()
        remote.assert_not_called()

if __name__ == "__main__":
    unittest.main()
