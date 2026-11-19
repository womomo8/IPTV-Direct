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


class PublicationValidationTests(unittest.TestCase):
    def test_empty_candidate_set_is_not_published(self):
        with tempfile.TemporaryDirectory() as directory:
            result = globalping._publish([], Path(directory), [], {}, [])
            self.assertEqual(result["status"], "blocked")
            self.assertFalse((Path(directory) / "iptv.m3u").exists())

    def test_channel_cap_is_enforced_in_output_validation(self):
        accepted = [
            {"channel": "CCTV-1", "normalized_url": "https://a.example/1"},
            {"channel": "CCTV-1", "normalized_url": "https://a.example/2"},
            {"channel": "CCTV-1", "normalized_url": "https://a.example/3"},
        ]
        m3u = "#EXTM3U\n" + "".join(f"#EXTINF:-1,{x['channel']}\n{x['normalized_url']}\n" for x in accepted)
        txt = "".join(f"{x['channel']},{x['normalized_url']}\n" for x in accepted)
        valid, reason = globalping._validate_new(m3u, txt, accepted, ["CCTV-1"])
        self.assertFalse(valid)
        self.assertIn("最多两条", reason)


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


if __name__ == "__main__":
    unittest.main()
