
import unittest
import urllib.error
from unittest.mock import patch

import globalping


CHANNELS = ["CCTV-1", "CCTV-5"]

SETTINGS = {
    "selection": {
        "max_routes_per_channel": 2,
    }
}


def make_candidate(
    url,
    channel="CCTV-1",
    media_type="HLS",
    remote_status="PASS",
):
    hls_local = "N/A" if media_type == "DIRECT_TS" else "PASS"
    hls_remote = (
        "N/A"
        if media_type == "DIRECT_TS"
        else remote_status
    )

    remote_checks = {
        "ipv4": {"status": remote_status},
        "http": {"status": remote_status},
        "hls": {"status": hls_remote},
        "ts": {"status": remote_status},
    }

    return {
        "channel": channel,
        "normalized_url": url,
        "media_type": media_type,
        "priority": 1,
        "source_order": 0,
        "checks": {
            "ipv4": {"status": "PASS"},
            "http": {"status": "PASS"},
            "hls": {"status": hls_local},
            "ts": {"status": "PASS"},
        },
        "globalping": {
            "status": remote_status,
            "checks": remote_checks,
        },
    }


class GlobalpingAdmissionTests(unittest.TestCase):

    def admit(self, candidates):
        return globalping.admit_candidates(
            candidates,
            CHANNELS,
            SETTINGS,
        )

    def test_acceptance_preserves_input_priority_order(self):
        first = make_candidate("https://example.test/first.m3u8")
        second = make_candidate("https://example.test/second.m3u8")

        result = self.admit([first, second])

        self.assertEqual(
            [
                item["normalized_url"]
                for item in result["accepted_routes"]
            ],
            [
                "https://example.test/first.m3u8",
                "https://example.test/second.m3u8",
            ],
        )

    def test_channel_keeps_at_most_two_routes(self):
        candidates = [
            make_candidate("https://example.test/one.m3u8"),
            make_candidate("https://example.test/two.m3u8"),
            make_candidate("https://example.test/three.m3u8"),
        ]

        result = self.admit(candidates)

        self.assertEqual(len(result["accepted_routes"]), 2)
        self.assertEqual(result["channel_counts"]["CCTV-1"], 2)
        self.assertEqual(
            [item["status"] for item in result["results"]],
            ["ACCEPTED", "ACCEPTED", "REJECTED"],
        )

    def test_other_channel_can_still_be_admitted(self):
        candidates = [
            make_candidate("https://example.test/one.m3u8"),
            make_candidate("https://example.test/two.m3u8"),
            make_candidate(
                "https://example.test/cctv5.m3u8",
                channel="CCTV-5",
            ),
        ]

        result = self.admit(candidates)

        self.assertEqual(len(result["accepted_routes"]), 3)
        self.assertEqual(result["channel_counts"]["CCTV-1"], 2)
        self.assertEqual(result["channel_counts"]["CCTV-5"], 1)

    def test_remote_fail_is_rejected(self):
        candidate = make_candidate(
            "https://example.test/fail.m3u8",
            remote_status="FAIL",
        )

        result = self.admit([candidate])

        self.assertEqual(result["accepted_count"], 0)
        self.assertEqual(result["results"][0]["status"], "REJECTED")

    def test_direct_ts_requires_remote_hls_na(self):
        candidate = make_candidate(
            "https://example.test/live.ts",
            media_type="DIRECT_TS",
        )

        result = self.admit([candidate])

        self.assertEqual(result["accepted_count"], 1)
        self.assertEqual(
            result["accepted_routes"][0]["globalping"]["checks"]["hls"]["status"],
            "N/A",
        )

    def test_duplicate_url_is_rejected(self):
        first = make_candidate("https://example.test/same.m3u8")
        second = make_candidate("https://example.test/same.m3u8")

        result = self.admit([first, second])

        self.assertEqual(result["accepted_count"], 1)
        self.assertEqual(result["rejected_count"], 1)

    def test_incomplete_remote_result_keeps_real_status(self):
        candidate = make_candidate(
            "https://example.test/incomplete.m3u8",
            remote_status="INCOMPLETE",
        )

        result = self.admit([candidate])

        self.assertEqual(result["accepted_count"], 1)
        self.assertEqual(
            result["accepted_routes"][0]["globalping"]["status"],
            "INCOMPLETE",
        )

    def test_pass_with_incomplete_remote_item_is_rejected(self):
        candidate = make_candidate(
            "https://example.test/inconsistent.m3u8",
        )
        candidate["globalping"]["checks"]["http"]["status"] = "INCOMPLETE"

        result = self.admit([candidate])

        self.assertEqual(result["accepted_count"], 0)
        self.assertEqual(result["rejected_count"], 1)

    def test_invalid_route_does_not_change_channel_count(self):
        candidate = make_candidate("not-a-url")

        result = self.admit([candidate])

        self.assertEqual(result["accepted_count"], 0)
        self.assertEqual(result["channel_counts"]["CCTV-1"], 0)
    def test_post_network_failure_is_marked_creation_uncertain(self):
        error = urllib.error.URLError("connection timed out")

        with patch.object(
            globalping.urllib.request,
            "urlopen",
            side_effect=error,
        ):
            with self.assertRaises(globalping.GlobalpingError) as caught:
                globalping._api_request(
                    f"{globalping.API_BASE}/measurements",
                    method="POST",
                    payload={},
                )

        self.assertTrue(caught.exception.creation_uncertain)
        self.assertFalse(caught.exception.rate_limited)

    def test_uncertain_creation_is_not_retried_and_reserves_budget(self):
        budget = {
            "remaining": 10,
            "probe_count": 1,
            "halted": False,
        }

        error = globalping.GlobalpingError(
            "创建请求超时，结果不确定",
            creation_uncertain=True,
        )

        with patch.object(
            globalping,
            "_api_request",
            side_effect=error,
        ) as request:
            with self.assertRaises(globalping.GlobalpingError):
                globalping._create_measurement(
                    "https://example.test/live.m3u8",
                    [{"limit": 1}],
                    budget,
                )

        request.assert_called_once()

        self.assertEqual(
            budget["creation_uncertain_count"],
            1,
        )
        self.assertEqual(budget["remaining"], 9)
        self.assertFalse(budget["halted"])

    def test_confirmed_creation_increments_measurement_count(self):
        budget = {
            "remaining": 10,
            "probe_count": 1,
            "halted": False,
        }

        with patch.object(
            globalping,
            "_api_request",
            return_value=(
                202,
                {"id": "measurement-1", "probesCount": 1},
                {},
            ),
        ):
            measurement_id = globalping._create_measurement(
                "https://example.test/live.m3u8",
                [{"limit": 1}],
                budget,
            )

        self.assertEqual(measurement_id, "measurement-1")
        self.assertEqual(budget["measurements_created"], 1)
        self.assertEqual(budget["remaining"], 9)
    def test_check_candidates_skips_capacity_candidate(self):
        first = make_candidate(
            "https://example.test/skip.ts",
            media_type="DIRECT_TS",
        )
        second = make_candidate(
            "https://example.test/check.ts",
            media_type="DIRECT_TS",
        )

        callback_results = []

        def skip_candidate(candidate):
            if candidate["normalized_url"].endswith("skip.ts"):
                return "该频道已达到最多保留 2 条线路的上限"
            return None

        remote_result = {
            "checks": {
                "ipv4": {"status": "PASS"},
                "http": {"status": "PASS"},
                "hls": {"status": "N/A"},
                "ts": {"status": "PASS"},
            },
            "stages": {},
            "probes": {},
            "measurements": [],
        }

        with patch.object(
            globalping,
            "read_limits",
            return_value={"remaining": 10},
        ), patch.object(
            globalping,
            "choose_probes",
            return_value=[{"limit": 1}],
        ), patch.object(
            globalping,
            "_run_direct_ts",
            return_value=remote_result,
        ) as run_direct_ts:
            results = globalping.check_candidates(
                [first, second],
                {"max_probes": 1},
                _skip_candidate=skip_candidate,
                _on_candidate_result=callback_results.append,
            )

        self.assertEqual(len(results), 2)
        self.assertEqual(
            results[0]["measurement_status"],
            "SKIPPED_CAPACITY",
        )
        self.assertEqual(
            results[1]["globalping"]["status"],
            globalping.PASS,
        )
        self.assertEqual(run_direct_ts.call_count, 1)
        self.assertEqual(callback_results, results)

if __name__ == "__main__":
    unittest.main()
    
