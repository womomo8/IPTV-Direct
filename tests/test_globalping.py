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


if __name__ == "__main__":
    unittest.main()
