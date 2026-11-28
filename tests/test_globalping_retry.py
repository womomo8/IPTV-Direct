"""Globalping no_probes_found 安全重试专项回归测试。

放入 tests/ 目录后运行：
    python -m unittest discover -s tests -p 'test_globalping_retry.py' -v

测试导入项目根目录中的 globalping.py；请先将修复版 globalping.py 放到项目根目录。
所有 API 均为模拟调用，不访问 Globalping 公网、不消耗额度。
"""

import json
import urllib.error
import unittest
from unittest.mock import Mock, patch

import globalping as gp


PROBE = {
    "country": "CN",
    "state": "Fujian",
    "city": "Xiamen",
    "asn": 9808,
    "network": "China Mobile",
    "tags": ["eyeball-network"],
}

MASTER_PLAYLIST = (
    b"#EXTM3U\n"
    b"#EXT-X-VERSION:3\n"
    b"#EXT-X-STREAM-INF:BANDWIDTH=1200000\n"
    b"variant/index.m3u8\n"
)


def finished_http(body=b"", status_code=200):
    if isinstance(body, bytes):
        body = body.decode("utf-8")
    return {
        "status": "finished",
        "resolvedAddress": "1.2.3.4",
        "statusCode": status_code,
        "headers": {},
        "rawBody": body,
        "truncated": False,
    }


def result_item(result, probe=None):
    return {"probe": dict(probe or PROBE), "result": dict(result)}


def measurement(*items):
    return {"status": "finished", "results": list(items)}


def no_probes_error():
    exc = gp.GlobalpingError("Globalping API HTTP 422：no_probes_found")
    exc.api_status_code = 422
    exc.api_error_type = "no_probes_found"
    exc.measurement_creation_no_probes = True
    return exc


class NoProbesApiClassificationTests(unittest.TestCase):
    def test_api_request_extracts_422_error_type(self):
        body = json.dumps({
            "error": {"type": "no_probes_found", "message": "No probes found"}
        }).encode("utf-8")
        error = urllib.error.HTTPError(
            "https://example.test/measurements", 422,
            "Unprocessable Entity", {"Content-Type": "application/json"}, None,
        )
        error.read = Mock(return_value=body)

        with patch.object(gp.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(gp.GlobalpingError) as caught:
                gp._api_request(
                    "https://example.test/measurements",
                    method="POST", payload={},
                )

        self.assertEqual(caught.exception.api_status_code, 422)
        self.assertEqual(caught.exception.api_error_type, "no_probes_found")
        # 仅 API 层解析错误；只有测量创建层才能赋予安全重试标记。
        self.assertFalse(caught.exception.measurement_creation_no_probes)

    def test_other_422_error_is_not_classified_as_no_probes(self):
        body = json.dumps({
            "error": {"type": "some_other_error", "message": "unexpected"}
        }).encode("utf-8")
        error = urllib.error.HTTPError(
            "https://example.test/measurements", 422,
            "Unprocessable Entity", {"Content-Type": "application/json"}, None,
        )
        error.read = Mock(return_value=body)

        with patch.object(gp.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(gp.GlobalpingError) as caught:
                gp._api_request(
                    "https://example.test/measurements",
                    method="POST", payload={},
                )

        self.assertEqual(caught.exception.api_status_code, 422)
        self.assertEqual(caught.exception.api_error_type, "some_other_error")
        self.assertFalse(caught.exception.measurement_creation_no_probes)

    def test_measurement_creation_no_probes_does_not_halt_budget(self):
        budget = {"remaining": 10, "probe_count": 1, "halted": False}
        error = no_probes_error()

        with patch.object(gp, "_remaining_budget", return_value=10), \
             patch.object(gp, "_api_request", side_effect=error):
            with self.assertRaises(gp.GlobalpingError) as caught:
                gp._create_measurement(
                    "http://example.test/live.ts",
                    [{"country": "CN", "asn": 101, "limit": 1}],
                    budget,
                )

        self.assertTrue(caught.exception.measurement_creation_no_probes)
        self.assertFalse(budget["halted"])


class NoProbesRetryFlowTests(unittest.TestCase):
    def test_initial_creation_no_probes_retries_another_asn(self):
        settings = {"max_probes": 3}
        candidate = {
            "id": "retry-candidate",
            "media_type": "DIRECT_TS",
            "normalized_url": "http://example.test/live.ts",
        }
        asns = iter([101, 102, 103, 104])
        selected = []
        actual_measurements = []

        def choose_one(max_probes=1, excluded_asns=None):
            asn = next(asns)
            selected.append((asn, set(excluded_asns or set())))
            return [{
                "country": "CN", "asn": asn,
                "tags": ["eyeball-network"], "limit": 1,
            }]

        def run_stage(url, locations, budget, headers=None, **kwargs):
            asn = locations[0]["asn"]
            if asn == 101:
                raise no_probes_error()
            actual_measurements.append(asn)
            probe = {**PROBE, "asn": asn}
            return (
                f"measurement-{asn}",
                measurement(result_item(finished_http(), probe=probe)),
            )

        with patch.object(
            gp, "read_limits",
            return_value={"limit": 50, "remaining": 20, "reset": 1},
        ), patch.object(gp, "choose_probes", side_effect=choose_one) as choose, \
             patch.object(gp, "_stage", side_effect=run_stage) as stage:
            session = gp.create_session(settings)
            output = gp.check_candidates([candidate], settings, session=session)

        result = output[0]["globalping"]
        self.assertEqual(result["status"], gp.PASS)
        self.assertEqual(actual_measurements, [102, 103, 104])
        self.assertEqual(
            [result["stages"][name]["asn"] for name in (
                "probe_1", "probe_2", "probe_3"
            )],
            [102, 103, 104],
        )
        self.assertEqual(len(result["measurements"]), 3)
        self.assertEqual(session["budget"]["unavailable_asns"], [101])
        self.assertFalse(session["budget"]["halted"])
        self.assertEqual(choose.call_count, 4)
        self.assertEqual(stage.call_count, 4)
        self.assertIn(101, selected[1][1])

    def test_hls_child_failure_does_not_retry_or_mark_asn_unavailable(self):
        candidate = {
            "id": "hls-child",
            "media_type": "HLS",
            "normalized_url": "http://example.test/master.m3u8",
        }
        budget = {
            "remaining": 10,
            "probe_count": 1,
            "halted": False,
            "unavailable_asns": [],
        }
        main = measurement(result_item(
            finished_http(MASTER_PLAYLIST),
            probe={**PROBE, "asn": 101},
        ))
        calls = []

        def run_stage(url, locations, current_budget, **kwargs):
            calls.append(url)
            if len(calls) == 1:
                return "main-id", main
            raise no_probes_error()

        with patch.object(gp, "_stage", side_effect=run_stage):
            result = gp._run_hls(
                candidate,
                [{"country": "CN", "asn": 101, "tags": ["eyeball-network"], "limit": 1}],
                budget,
                asn=101,
            )

        self.assertEqual(len(calls), 2)
        self.assertEqual(result["measurements"], ["main-id"])
        self.assertEqual(result["checks"]["hls"]["status"], gp.INCOMPLETE)
        self.assertEqual(budget["unavailable_asns"], [])
        self.assertFalse(budget["halted"])

    def test_non_no_probes_exception_still_halts_shared_budget(self):
        settings = {"max_probes": 3}
        candidates = [
            {"id": "a", "media_type": "DIRECT_TS", "normalized_url": "http://example.test/a.ts"},
            {"id": "b", "media_type": "DIRECT_TS", "normalized_url": "http://example.test/b.ts"},
        ]
        stage_calls = []

        def choose_one(*args, **kwargs):
            return [{"country": "CN", "asn": 101, "tags": ["eyeball-network"], "limit": 1}]

        def count_stage(*args, **kwargs):
            stage_calls.append(1)
            return original_stage(*args, **kwargs)

        original_stage = gp._stage
        with patch.object(
            gp, "read_limits",
            return_value={"limit": 50, "remaining": 20, "reset": 1},
        ), patch.object(gp, "_remaining_budget", return_value=20), \
             patch.object(gp, "choose_probes", side_effect=choose_one), \
             patch.object(
                 gp, "_api_request",
                 side_effect=gp.GlobalpingError("网络超时，无法确认测量是否创建"),
             ), patch.object(gp, "_stage", side_effect=count_stage):
            session = gp.create_session(settings)
            output = gp.check_candidates(candidates, settings, session=session)

        self.assertTrue(session["budget"]["halted"])
        self.assertEqual(len(stage_calls), 1)
        self.assertEqual(output[0]["globalping"]["status"], gp.INCOMPLETE)
        self.assertEqual(output[1]["globalping"]["status"], gp.INCOMPLETE)


if __name__ == "__main__":
    unittest.main()
