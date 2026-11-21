import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import globalping


class GlobalpingAggregationTests(unittest.TestCase):
    def test_http_any_valid_success_wins(self):
        measurement = {"results": [
            {"probe": {"city": "A"}, "result": {"status": "finished", "statusCode": 200, "rawBody": "ok"}},
            {"probe": {"city": "B"}, "result": {"status": "finished", "statusCode": 503}},
        ]}
        state, raw, success = globalping._http_aggregate(measurement)
        self.assertEqual(state, globalping.PASS)
        self.assertEqual(len(success), 1)
        self.assertEqual(len(raw), 2)

    def test_partial_206_http_response_is_incomplete(self):
        measurement = {"results": [
            {"probe": {"city": "A"}, "result": {
                "status": "finished", "statusCode": 206,
                "headers": {"content-range": "bytes 0-1023/5000", "content-length": "1024"},
                "rawBody": "x" * 1024,
            }},
        ]}
        state, _, success = globalping._http_aggregate(measurement)
        self.assertEqual(state, globalping.INCOMPLETE)
        self.assertEqual(success, [])

    def test_http_probe_failure_without_target_response_is_incomplete(self):
        measurement = {"results": [
            {"probe": {"city": "A"}, "result": {"status": "offline", "rawOutput": "probe offline"}},
        ]}
        state, _, success = globalping._http_aggregate(measurement)
        self.assertEqual(state, globalping.INCOMPLETE)
        self.assertEqual(success, [])

    def test_partial_206_manifest_is_not_success(self):
        record = {"status_code": 206, "body": "#EXTM3U\n#EXT-X-TARGETDURATION:6\n",
                  "headers": {"content-range": "bytes 0-30/200", "content-length": "30"}}
        complete, reason = globalping._hls_content_complete(record)
        self.assertFalse(complete)
        self.assertTrue(reason)

    def test_complete_206_manifest_requires_full_range_and_length(self):
        body = "#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXTINF:6,\nseg.ts\n"
        size = len(body.encode())
        record = {"status_code": 206, "body": body,
                  "headers": {"content-range": f"bytes 0-{size-1}/{size}", "content-length": str(size)}}
        complete, _ = globalping._hls_content_complete(record)
        self.assertTrue(complete)

    def test_master_playlist_child_is_resolved(self):
        body = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=100000\nlow/index.m3u8\n"
        record = {"status_code": 200, "body": body, "headers": {"content-length": str(len(body))}}
        self.assertEqual(globalping._master_child_url([record], "https://host.example/master.m3u8"),
                         "https://host.example/low/index.m3u8")

    def test_hls_probe_error_without_target_failure_evidence_is_incomplete(self):
        record = {
            "probe": {"city": "A"},
            "status": "error",
            "status_code": 503,
            "target_failure_evidence": False,
        }
        state, evidence = globalping._hls_aggregate([], [record])
        self.assertEqual(state, globalping.INCOMPLETE)
        self.assertEqual(evidence[0]["status"], globalping.INCOMPLETE)

    def test_hls_confirmed_target_failure_is_fail(self):
        record = {
            "probe": {"city": "A"},
            "status": "finished",
            "status_code": 503,
            "target_failure_evidence": True,
        }
        state, evidence = globalping._hls_aggregate([], [record])
        self.assertEqual(state, globalping.FAIL)
        self.assertEqual(evidence[0]["status"], globalping.INCOMPLETE)

    def test_hls_valid_success_wins_over_other_probe_failure(self):
        body = "#EXTM3U\n#EXTINF:6,\nsegment.ts\n"
        success = {
            "probe": {"city": "A"},
            "status": "finished",
            "status_code": 200,
            "headers": {"content-length": str(len(body))},
            "body": body,
            "target_failure_evidence": False,
        }
        failure = {
            "probe": {"city": "B"},
            "status": "finished",
            "status_code": 503,
            "target_failure_evidence": True,
        }
        state, evidence = globalping._hls_aggregate(
            [success], [success, failure]
        )
        self.assertEqual(state, globalping.PASS)
        self.assertEqual(evidence[0]["status"], globalping.PASS)


class GlobalpingDryRunTests(unittest.TestCase):
    def test_dry_run_never_calls_api_or_publishes(self):
        candidate = {"channel": "CCTV-1", "normalized_url": "https://example.com/live.m3u8"}
        with patch.object(globalping, "_api_request", side_effect=AssertionError("API called")), \
             patch.object(globalping, "_publish", side_effect=AssertionError("publish called")):
            result = globalping.run_globalping(
                [candidate], {"globalping": {"max_probes": 3}}, ["CCTV-1"], dry_run=True
            )
        self.assertEqual(result["publication"]["status"], "not_run")
        self.assertEqual(result["input_queue_identities"], [("CCTV-1", "https://example.com/live.m3u8")])

class DirectTSAndQuotaTests(unittest.TestCase):
    def test_direct_ts_uses_http_only_and_consumes_actual_probe_quota(self):
        candidate = {
            "channel": "CCTV-1",
            "media_type": "DIRECT_TS",
            "normalized_url": "https://example.com/live.ts",
        }
        budget = {"remaining": 4, "stop_new": 0}
        stats = globalping.Counter()

        with patch.object(
            globalping,
            "_create_measurement",
            return_value=("measurement-1", 3),
        ) as create, patch.object(
            globalping,
            "_poll",
            return_value=({"status": "finished"}, None),
        ), patch.object(
            globalping,
            "_http_aggregate",
            return_value=(globalping.PASS, [], [{"probe": {"city": "A"}}]),
        ):
            result = globalping._candidate_remote(
                candidate, [], 3, 8, budget, stats
            )

        self.assertEqual(create.call_count, 1)
        self.assertEqual(create.call_args.args[0], "http")
        self.assertEqual(create.call_args.args[3], 3)
        self.assertEqual(budget["remaining"], 1)
        self.assertEqual(result["checks"]["ipv4"], globalping.NA)
        self.assertEqual(result["checks"]["http"], globalping.PASS)
        self.assertEqual(result["checks"]["hls"], globalping.NA)
        self.assertEqual(result["overall"], globalping.PASS)
        self.assertEqual(stats["actual_measurements"], 1)
        self.assertEqual(stats["actual_probe_slots"], 3)

    def test_probe_request_is_limited_by_remaining_test_quota(self):
        candidate = {
            "channel": "CCTV-1",
            "media_type": "DIRECT_TS",
            "normalized_url": "https://example.com/live.ts",
        }
        budget = {"remaining": 2, "stop_new": 0}
        stats = globalping.Counter()

        with patch.object(
            globalping,
            "_create_measurement",
            return_value=("measurement-1", 2),
        ) as create, patch.object(
            globalping,
            "_poll",
            return_value=({"status": "finished"}, None),
        ), patch.object(
            globalping,
            "_http_aggregate",
            return_value=(globalping.PASS, [], [{"probe": {"city": "A"}}]),
        ):
            result = globalping._candidate_remote(
                candidate, [], 3, 8, budget, stats
            )

        self.assertEqual(create.call_args.args[3], 2)
        self.assertEqual(budget["remaining"], 0)
        self.assertEqual(result["overall"], globalping.PASS)
        self.assertEqual(stats["actual_measurements"], 1)
        self.assertEqual(stats["actual_probe_slots"], 2)

    def test_hls_second_stage_is_blocked_without_full_probe_quota(self):
        candidate = {
            "channel": "CCTV-1",
            "media_type": "HLS",
            "normalized_url": "https://example.com/master.m3u8",
        }
        budget = {"remaining": 8, "stop_new": 0}
        stats = globalping.Counter()
        probe = {"probe": {"city": "A", "country": "CN", "asn": 9808}}

        with patch.object(
            globalping,
            "_create_measurement",
            side_effect=[
                ("ipv4-measurement", 3),
                ("http-measurement", 3),
            ],
        ) as create, patch.object(
            globalping,
            "_poll",
            return_value=({"status": "finished"}, None),
        ), patch.object(
            globalping,
            "_ping_aggregate",
            return_value=(globalping.PASS, [probe]),
        ), patch.object(
            globalping,
            "_http_aggregate",
            return_value=(globalping.PASS, [probe], [probe]),
        ), patch.object(
            globalping,
            "_master_child_url",
            return_value="https://example.com/child.m3u8",
        ):
            result = globalping._candidate_remote(
                candidate, [], 3, 8, budget, stats
            )

        self.assertEqual(create.call_count, 2)
        self.assertEqual(budget["remaining"], 2)
        self.assertEqual(result["checks"]["hls"], globalping.INCOMPLETE)
        self.assertEqual(result["overall"], globalping.INCOMPLETE)
        self.assertEqual(stats["actual_measurements"], 2)
        self.assertEqual(stats["actual_probe_slots"], 6)

    def test_hls_second_stage_uncertain_creation_blocks_admission(self):
        candidate = {
            "channel": "CCTV-1",
            "media_type": "HLS",
            "normalized_url": "https://example.com/master.m3u8",
        }
        budget = {"remaining": 9, "stop_new": 0}
        stats = globalping.Counter()
        probe = {"probe": {"city": "A", "country": "CN", "asn": 9808}}

        with patch.object(
            globalping,
            "_create_measurement",
            side_effect=[
                ("ipv4-measurement", 3),
                ("http-measurement", 3),
                globalping.GlobalpingError(
                    "connection lost", uncertain=True
                ),
            ],
        ) as create, patch.object(
            globalping,
            "_poll",
            return_value=({"status": "finished"}, None),
        ), patch.object(
            globalping,
            "_ping_aggregate",
            return_value=(globalping.PASS, [probe]),
        ), patch.object(
            globalping,
            "_http_aggregate",
            return_value=(globalping.PASS, [probe], [probe]),
        ), patch.object(
            globalping,
            "_master_child_url",
            return_value="https://example.com/child.m3u8",
        ):
            result = globalping._candidate_remote(
                candidate, [], 3, 8, budget, stats
            )

        self.assertEqual(create.call_count, 3)
        self.assertTrue(result["creation_uncertain"])
        self.assertEqual(result["checks"]["hls"], globalping.INCOMPLETE)
        self.assertEqual(result["overall"], globalping.INCOMPLETE)
        self.assertEqual(stats["creation_uncertain_candidates"], 1)
        self.assertEqual(stats["actual_measurements"], 2)
        self.assertEqual(stats["actual_probe_slots"], 6)
        self.assertEqual(budget["remaining"], 0)

    def _run_hls_child_probe_case(self, child_probe):
        candidate = {
            "channel": "CCTV-1",
            "media_type": "HLS",
            "normalized_url": "https://example.com/master.m3u8",
        }
        parent_probe = {
            "probe": {
                "city": "A",
                "country": "CN",
                "asn": 9808,
                "network": "China Mobile",
            }
        }
        body = "#EXTM3U\n#EXTINF:6,\nsegment.ts\n"

        def make_record(probe):
            return {
                "probe": probe["probe"],
                "status": "finished",
                "status_code": 200,
                "headers": {"content-length": str(len(body))},
                "body": body,
                "success_evidence": True,
            }

        parent_record = make_record(parent_probe)
        child_record = make_record(child_probe)
        budget = {"remaining": 9, "stop_new": 0}
        stats = globalping.Counter()

        with patch.object(
            globalping,
            "_create_measurement",
            side_effect=[
                ("ipv4-measurement", 3),
                ("http-measurement", 3),
                ("child-measurement", 3),
            ],
        ) as create, patch.object(
            globalping,
            "_poll",
            return_value=({"status": "finished"}, None),
        ), patch.object(
            globalping,
            "_ping_aggregate",
            return_value=(globalping.PASS, [parent_probe]),
        ), patch.object(
            globalping,
            "_http_aggregate",
            side_effect=[
                (globalping.PASS, [parent_record], [parent_record]),
                (globalping.PASS, [child_record], [child_record]),
            ],
        ), patch.object(
            globalping,
            "_master_child_url",
            return_value="https://example.com/child.m3u8",
        ):
            result = globalping._candidate_remote(
                candidate, [], 3, 8, budget, stats
            )

        return result, budget, stats, create

    def test_hls_second_stage_reuses_same_probe_set(self):
        child_probe = {
            "probe": {
                "city": "A",
                "country": "CN",
                "asn": 9808,
                "network": "China Mobile",
            }
        }

        result, budget, stats, create = self._run_hls_child_probe_case(
            child_probe
        )

        self.assertEqual(create.call_count, 3)
        self.assertEqual(
            create.call_args_list[2].kwargs["previous_measurement"],
            "http-measurement",
        )
        self.assertEqual(create.call_args_list[2].args[1],
                         "https://example.com/child.m3u8")
        self.assertEqual(budget["remaining"], 0)
        self.assertEqual(result["checks"]["hls"], globalping.PASS)
        self.assertEqual(result["overall"], globalping.PASS)
        self.assertEqual(stats["actual_measurements"], 3)
        self.assertEqual(stats["actual_probe_slots"], 9)

    def test_hls_second_stage_probe_mismatch_stays_incomplete(self):
        child_probe = {
            "probe": {
                "city": "B",
                "country": "CN",
                "asn": 9808,
                "network": "China Mobile",
            }
        }

        result, budget, stats, create = self._run_hls_child_probe_case(
            child_probe
        )

        self.assertEqual(create.call_count, 3)
        self.assertEqual(
            create.call_args_list[2].kwargs["previous_measurement"],
            "http-measurement",
        )
        self.assertEqual(budget["remaining"], 0)
        self.assertEqual(result["checks"]["hls"], globalping.INCOMPLETE)
        self.assertEqual(result["overall"], globalping.INCOMPLETE)
        self.assertTrue(any(
            "相同探针" in reason
            for reason in result.get("reasons", [])
        ))
        self.assertEqual(stats["actual_measurements"], 3)
        self.assertEqual(stats["actual_probe_slots"], 9)

    def test_hls_child_measurement_uses_previous_measurement_id(self):
        with patch.object(
            globalping,
            "_api_request",
            return_value=(202, {"id": "child-measurement", "probesCount": 3}, {}),
        ) as request:
            measurement_id, probes_count = globalping._create_measurement(
                "http",
                "https://example.com/child.m3u8",
                [],
                3,
                previous_measurement="http-measurement",
            )

        payload = request.call_args.args[2]
        self.assertEqual(measurement_id, "child-measurement")
        self.assertEqual(probes_count, 3)
        self.assertEqual(payload["locations"], "http-measurement")
        self.assertNotIn("limit", payload)

class BudgetBoundaryAndExceptionTests(unittest.TestCase):
    def _candidate(self):
        return {
            "channel": "CCTV-1",
            "media_type": "DIRECT_TS",
            "normalized_url": "https://example.com/live.ts",
        }

    def test_no_probes_refunds_reserved_quota(self):
        budget = {"remaining": 3, "stop_new": 0}
        stats = globalping.Counter()

        with patch.object(
            globalping,
            "_create_measurement",
            side_effect=globalping.GlobalpingError(
                "no probes", no_probes=True
            ),
        ) as create:
            result = globalping._candidate_remote(
                self._candidate(), [], 3, 8, budget, stats
            )

        self.assertEqual(create.call_count, 1)
        self.assertEqual(budget["remaining"], 3)
        self.assertEqual(stats["actual_measurements"], 0)
        self.assertEqual(stats["actual_probe_slots"], 0)
        self.assertEqual(result["overall"], globalping.INCOMPLETE)

    def test_http_429_refunds_quota_and_stops_new_measurements(self):
        budget = {"remaining": 3, "stop_new": 0}
        stats = globalping.Counter()

        with patch.object(
            globalping,
            "_create_measurement",
            side_effect=globalping.GlobalpingError(
                "rate limited", status=429
            ),
        ) as create:
            result = globalping._candidate_remote(
                self._candidate(), [], 3, 8, budget, stats
            )

        self.assertEqual(create.call_count, 1)
        self.assertEqual(budget["remaining"], 3)
        self.assertEqual(budget["stop_new"], 1)
        self.assertEqual(stats["actual_measurements"], 0)
        self.assertEqual(stats["actual_probe_slots"], 0)
        self.assertEqual(result["overall"], globalping.INCOMPLETE)

    def test_uncertain_creation_keeps_reserved_quota_separate(self):
        budget = {"remaining": 3, "stop_new": 0}
        stats = globalping.Counter()

        with patch.object(
            globalping,
            "_create_measurement",
            side_effect=globalping.GlobalpingError(
                "connection lost", uncertain=True
            ),
        ) as create:
            result = globalping._candidate_remote(
                self._candidate(), [], 3, 8, budget, stats
            )

        self.assertEqual(create.call_count, 1)
        self.assertEqual(budget["remaining"], 0)
        self.assertTrue(result["creation_uncertain"])
        self.assertEqual(stats["creation_uncertain_candidates"], 1)
        self.assertEqual(stats["quota_blocked_candidates"], 0)
        self.assertEqual(stats["actual_measurements"], 0)
        self.assertEqual(stats["actual_probe_slots"], 0)

    def test_known_creation_failure_refunds_quota(self):
        budget = {"remaining": 3, "stop_new": 0}
        stats = globalping.Counter()

        with patch.object(
            globalping,
            "_create_measurement",
            side_effect=globalping.GlobalpingError("creation failed"),
        ):
            result = globalping._candidate_remote(
                self._candidate(), [], 3, 8, budget, stats
            )

        self.assertEqual(budget["remaining"], 3)
        self.assertEqual(stats["actual_measurements"], 0)
        self.assertEqual(stats["actual_probe_slots"], 0)
        self.assertEqual(result["overall"], globalping.INCOMPLETE)

    def test_poll_boundary_keeps_created_measurement_in_statistics(self):
        budget = {"remaining": 3, "stop_new": 0}
        stats = globalping.Counter()

        with patch.object(
            globalping,
            "_create_measurement",
            return_value=("measurement-1", 2),
        ), patch.object(
            globalping,
            "_poll",
            return_value=({"status": "in-progress"}, "轮询边界耗尽"),
        ), patch.object(
            globalping,
            "_http_aggregate",
            return_value=(globalping.INCOMPLETE, [], []),
        ):
            result = globalping._candidate_remote(
                self._candidate(), [], 3, 8, budget, stats
            )

        self.assertEqual(stats["actual_measurements"], 1)
        self.assertEqual(stats["actual_probe_slots"], 2)
        self.assertEqual(stats["incomplete_measurements"], 1)
        self.assertEqual(stats["completed_measurements"], 0)
        self.assertEqual(len(result["measurements"]), 1)
        self.assertEqual(result["overall"], globalping.INCOMPLETE)
        self.assertTrue(any(
            "轮询边界耗尽" in reason
            for reason in result.get("reasons", [])
        ))

    def test_retry_after_does_not_extend_poll_boundary(self):
        error = globalping.urllib.error.HTTPError(
            "https://api.globalping.io/v1/measurements/test",
            429,
            "Too Many Requests",
            {"Retry-After": "2"},
            None,
        )

        with patch.object(
            globalping.urllib.request,
            "urlopen",
            side_effect=error,
        ) as request, patch.object(
            globalping.time,
            "monotonic",
            return_value=0.1,
        ), patch.object(
            globalping.time,
            "sleep",
        ) as sleep:
            measurement, reason = globalping._poll("measurement-test", 1)

        self.assertIsNone(measurement)
        self.assertEqual(reason, "轮询边界耗尽")
        self.assertEqual(request.call_count, 1)
        sleep.assert_not_called()

class GlobalpingCreditsTests(unittest.TestCase):
    def test_rate_limit_and_credits_are_combined(self):
        with patch.object(
            globalping,
            "_api_request",
            return_value=(200, {
                "rateLimit": {
                    "measurements": {
                        "create": {
                            "limit": 100,
                            "remaining": 12,
                            "reset": 12345,
                        }
                    }
                },
                "credits": {"remaining": 34},
            }, {}),
        ):
            limits = globalping._read_limits()

        self.assertEqual(limits["limit"], 100)
        self.assertEqual(limits["rate_remaining"], 12)
        self.assertEqual(limits["credits_remaining"], 34)
        self.assertEqual(limits["remaining"], 46)
        self.assertEqual(limits["reset"], 12345)

    def test_missing_credits_defaults_to_zero(self):
        with patch.object(
            globalping,
            "_api_request",
            return_value=(200, {
                "rateLimit": {
                    "measurements": {
                        "create": {
                            "limit": 100,
                            "remaining": 12,
                            "reset": 12345,
                        }
                    }
                }
            }, {}),
        ):
            limits = globalping._read_limits()

        self.assertEqual(limits["rate_remaining"], 12)
        self.assertEqual(limits["credits_remaining"], 0)
        self.assertEqual(limits["remaining"], 12)

    def test_negative_remaining_values_are_clamped_to_zero(self):
        with patch.object(
            globalping,
            "_api_request",
            return_value=(200, {
                "rateLimit": {
                    "measurements": {
                        "create": {
                            "limit": 100,
                            "remaining": -5,
                            "reset": 12345,
                        }
                    }
                },
                "credits": {"remaining": -8},
            }, {}),
        ):
            limits = globalping._read_limits()

        self.assertEqual(limits["rate_remaining"], 0)
        self.assertEqual(limits["credits_remaining"], 0)
        self.assertEqual(limits["remaining"], 0)

    def test_invalid_credits_format_stops_new_measurements(self):
        with patch.object(
            globalping,
            "_api_request",
            return_value=(200, {
                "rateLimit": {
                    "measurements": {
                        "create": {
                            "limit": 100,
                            "remaining": 12,
                            "reset": 12345,
                        }
                    }
                },
                "credits": ["invalid"],
            }, {}),
        ):
            with self.assertRaises(globalping.GlobalpingError):
                globalping._read_limits()

    def test_invalid_credits_remaining_stops_new_measurements(self):
        with patch.object(
            globalping,
            "_api_request",
            return_value=(200, {
                "rateLimit": {
                    "measurements": {
                        "create": {
                            "limit": 100,
                            "remaining": 12,
                            "reset": 12345,
                        }
                    }
                },
                "credits": {"remaining": "invalid"},
            }, {}),
        ):
            with self.assertRaises(globalping.GlobalpingError):
                globalping._read_limits()


class RemoteAdmissionTests(unittest.TestCase):
    def _candidate(self, media="HLS"):
        return {"media_type": media}

    def test_hls_requires_all_applicable_checks_to_pass(self):
        remote = {
            "checks": {
                "ipv4": globalping.PASS,
                "http": globalping.PASS,
                "hls": globalping.PASS,
            },
            "overall": globalping.PASS,
        }
        eligible, _ = globalping._remote_admission(
            self._candidate(), remote
        )
        self.assertTrue(eligible)

    def test_direct_ts_requires_http_pass_and_other_checks_na(self):
        remote = {
            "checks": {
                "ipv4": globalping.NA,
                "http": globalping.PASS,
                "hls": globalping.NA,
            },
            "overall": globalping.PASS,
        }
        eligible, _ = globalping._remote_admission(
            self._candidate("DIRECT_TS"), remote
        )
        self.assertTrue(eligible)

    def test_incomplete_unknown_fail_and_missing_states_are_rejected(self):
        for state in (
            globalping.INCOMPLETE,
            "UNKNOWN",
            globalping.FAIL,
            None,
        ):
            with self.subTest(state=state):
                remote = {
                    "checks": {
                        "ipv4": globalping.PASS,
                        "http": state,
                        "hls": globalping.PASS,
                    },
                    "overall": globalping.PASS,
                }
                eligible, _ = globalping._remote_admission(
                    self._candidate(), remote
                )
                self.assertFalse(eligible)

    def test_missing_applicable_check_is_rejected(self):
        remote = {
            "checks": {
                "ipv4": globalping.PASS,
                "http": globalping.PASS,
            },
            "overall": globalping.PASS,
        }
        eligible, _ = globalping._remote_admission(
            self._candidate(), remote
        )
        self.assertFalse(eligible)

    def test_overall_state_must_be_pass(self):
        remote = {
            "checks": {
                "ipv4": globalping.PASS,
                "http": globalping.PASS,
                "hls": globalping.PASS,
            },
            "overall": globalping.INCOMPLETE,
        }
        eligible, _ = globalping._remote_admission(
            self._candidate(), remote
        )
        self.assertFalse(eligible)

    def test_direct_ts_rejects_invalid_na_states(self):
        remote = {
            "checks": {
                "ipv4": globalping.PASS,
                "http": globalping.PASS,
                "hls": globalping.NA,
            },
            "overall": globalping.PASS,
        }
        eligible, _ = globalping._remote_admission(
            self._candidate("DIRECT_TS"), remote
        )
        self.assertFalse(eligible)


if __name__ == "__main__":
    unittest.main()

