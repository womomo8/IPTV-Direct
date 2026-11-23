"""IPTV-Direct globalping.py 专项测试。

仅使用模拟 API 响应和本地数据，不访问 Globalping 公网 API，
不创建真实测量，不消耗额度，不修改正式订阅。

运行方式：
    python -m unittest discover -s tests -p "test_globalping.py" -v
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
    "latitude": 24.5,
    "longitude": 118.1,
    "tags": ["eyeball-network"],
    "resolvers": ["1.1.1.1"],
}

MEDIA_PLAYLIST = (
    "#EXTM3U\n"
    "#EXT-X-VERSION:3\n"
    "#EXT-X-TARGETDURATION:10\n"
    "#EXT-X-MEDIA-SEQUENCE:1\n"
    "#EXTINF:10,\n"
    "segment.ts\n"
    "#EXT-X-ENDLIST\n"
).encode()

MASTER_PLAYLIST = (
    "#EXTM3U\n"
    "#EXT-X-VERSION:3\n"
    "#EXT-X-STREAM-INF:BANDWIDTH=1200000\n"
    "variant/index.m3u8\n"
).encode()


def valid_ts(length=1024, offset=0):
    data = bytearray(b"A" * length)
    for index in range(5):
        position = offset + index * 188
        if position < length:
            data[position] = 0x47
    return bytes(data)


def result_item(result, probe=None):
    return {"probe": dict(probe or PROBE), "result": dict(result)}


def finished_http(body, status_code=200, headers=None, **extra):
    value = {
        "status": "finished",
        "resolvedAddress": "1.2.3.4",
        "statusCode": status_code,
        "headers": headers or {},
        "rawBody": body.decode("utf-8") if isinstance(body, bytes) else body,
        "truncated": False,
    }
    value.update(extra)
    return value


def measurement(*items):
    return {"status": "finished", "results": list(items)}


class ApiRequestTests(unittest.TestCase):
    def test_json_array_response_is_supported(self):
        response = Mock()
        response.status = 200
        response.headers = {"Content-Type": "application/json"}
        response.read.return_value = b'[{"id":"p1"}]'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch.object(gp.urllib.request, "urlopen", return_value=response):
            status, data, headers = gp._api_request("https://example.test/probes")
        self.assertEqual(status, 200)
        self.assertEqual(data, [{"id": "p1"}])

    def test_304_is_normal_response(self):
        error = urllib.error.HTTPError(
            "https://example.test/measurements/id", 304, "Not Modified",
            {"ETag": '"abc"'}, None,
        )
        with patch.object(gp.urllib.request, "urlopen", side_effect=error):
            status, data, headers = gp._api_request("https://example.test/measurements/id")
        self.assertEqual(status, 304)
        self.assertIsNone(data)
        self.assertEqual(headers.get("etag"), '"abc"')

    def test_429_is_marked_rate_limited(self):
        error = urllib.error.HTTPError(
            "https://example.test/measurements", 429, "Too Many Requests",
            {"Retry-After": "10"}, None,
        )
        with patch.object(gp.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(gp.GlobalpingError) as caught:
                gp._api_request("https://example.test/measurements", method="POST", payload={})
        self.assertTrue(caught.exception.rate_limited)
        self.assertIn("429", str(caught.exception))

    def test_read_limits_parses_live_quota(self):
        payload = {"rateLimit": {"measurements": {"create": {
            "limit": 100, "remaining": 7, "reset": 12345,
        }}}}
        with patch.object(gp, "_api_request", return_value=(200, payload, {})):
            self.assertEqual(gp.read_limits(), {"limit": 100, "remaining": 7, "reset": 12345})

    def test_read_limits_rejects_unknown_shape(self):
        with patch.object(gp, "_api_request", return_value=(200, [], {})):
            with self.assertRaises(gp.GlobalpingError):
                gp.read_limits()


class ProbeSelectionTests(unittest.TestCase):
    def test_selects_mainland_eyeball_probes_mobile_first_and_max_three(self):
        probes = [
            {"tags": ["eyeball-network"], "location": {"country": "CN", "asn": 9808, "network": "China Mobile"}},
            {"tags": ["eyeball-network"], "location": {"country": "CN", "asn": 4134, "network": "China Telecom"}},
            {"tags": ["eyeball-network"], "location": {"country": "CN", "asn": 4837, "network": "China Unicom"}},
            {"tags": ["eyeball-network"], "location": {"country": "US", "asn": 9808, "network": "China Mobile"}},
            {"tags": ["hosting"], "location": {"country": "CN", "asn": 9808, "network": "China Mobile"}},
        ]
        with patch.object(gp, "_api_request", return_value=(200, probes, {})):
            locations = gp.choose_probes(3)
        self.assertEqual(sum(item["limit"] for item in locations), 3)
        self.assertLessEqual(sum(item["limit"] for item in locations), 3)
        self.assertEqual(locations[0]["asn"], 9808)
        self.assertTrue(all(item["country"] == "CN" for item in locations))
        self.assertTrue(all(item["tags"] == ["eyeball-network"] for item in locations))

    def test_rejects_invalid_probe_limit(self):
        for value in (0, -1, True, "3"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    gp.choose_probes(value)

    def test_rejects_non_array_probe_response(self):
        with patch.object(gp, "_api_request", return_value=(200, {"data": []}, {})):
            with self.assertRaises(gp.GlobalpingError):
                gp.choose_probes(3)

    def test_no_eligible_probe_is_incomplete(self):
        with patch.object(gp, "_api_request", return_value=(200, [
            {"tags": ["eyeball-network"], "location": {"country": "US", "asn": 9808}}
        ], {})):
            with self.assertRaises(gp.GlobalpingError):
                gp.choose_probes(3)


class UrlAndBodyTests(unittest.TestCase):
    def test_split_url_preserves_path_query_and_port(self):
        parts = gp._split_url("https://example.test:8443/live/index.m3u8?token=x")
        self.assertEqual(parts["target"], "example.test")
        self.assertEqual(parts["protocol"], "HTTPS")
        self.assertEqual(parts["port"], 8443)
        self.assertEqual(parts["path"], "/live/index.m3u8")
        self.assertEqual(parts["query"], "token=x")

    def test_ipv4_literal_measurement_omits_ip_version(self):
        budget = {"remaining": 10, "probe_count": 1}
        locations = [{"country": "CN", "asn": 9808, "limit": 1}]

        with patch.object(gp, "_remaining_budget", return_value=10), \
             patch.object(gp, "_api_request", return_value=(202, {"id": "m1", "probesCount": 1}, {})) as request:
            measurement_id = gp._create_measurement(
                "http://1.2.3.4/live.ts",
                locations,
                budget,
            )

        self.assertEqual(measurement_id, "m1")
        payload = request.call_args.kwargs["payload"]
        self.assertNotIn("ipVersion", payload["measurementOptions"])

    def test_domain_measurement_keeps_ipv4_version(self):
        budget = {"remaining": 10, "probe_count": 1}
        locations = [{"country": "CN", "asn": 9808, "limit": 1}]

        with patch.object(gp, "_remaining_budget", return_value=10), \
             patch.object(gp, "_api_request", return_value=(202, {"id": "m1", "probesCount": 1}, {})) as request:
            measurement_id = gp._create_measurement(
                "http://example.test/live.ts",
                locations,
                budget,
            )

        self.assertEqual(measurement_id, "m1")
        payload = request.call_args.kwargs["payload"]
        self.assertEqual(payload["measurementOptions"]["ipVersion"], 4)

    def test_split_url_rejects_ipv6_literal(self):
        with self.assertRaises(gp.GlobalpingError):
            gp._split_url("http://[2001:db8::1]/live.ts")

    def test_split_url_rejects_credentials_and_unsupported_scheme(self):
        for url in ("http://user:pass@example.test/a", "ftp://example.test/a"):
            with self.subTest(url=url):
                with self.assertRaises(gp.GlobalpingError):
                    gp._split_url(url)

    def test_raw_body_rejects_missing_truncated_and_lossy_text(self):
        self.assertIsNone(gp._raw_body_bytes({}))
        self.assertIsNone(gp._raw_body_bytes({"rawBody": "abc", "truncated": True}))
        self.assertIsNone(gp._raw_body_bytes({"rawBody": "a\ufffdb", "truncated": False}))

    def test_ts_sync_accepts_nonzero_packet_alignment(self):
        self.assertTrue(gp._ts_sync_ok(valid_ts(offset=17)))

    def test_ts_sync_rejects_short_or_invalid_data(self):
        self.assertFalse(gp._ts_sync_ok(b"A" * 752))
        self.assertFalse(gp._ts_sync_ok(b"A" * 1024))


class PlaylistTests(unittest.TestCase):
    def test_parses_media_playlist_and_resolves_segment(self):
        parsed = gp._parse_playlist(MEDIA_PLAYLIST, "https://example.test/live/index.m3u8")
        self.assertEqual(parsed["kind"], "media")
        self.assertEqual(parsed["segments"], ["https://example.test/live/segment.ts"])

    def test_parses_master_playlist_and_resolves_variant(self):
        parsed = gp._parse_playlist(MASTER_PLAYLIST, "https://example.test/live/master.m3u8")
        self.assertEqual(parsed["kind"], "master")
        self.assertEqual(parsed["child_url"], "https://example.test/live/variant/index.m3u8")

    def test_rejects_channel_subscription_m3u(self):
        body = b"#EXTM3U\n#EXTINF:-1,CCTV-5\nhttp://example.test/live.ts\n"
        with self.assertRaises(gp.GlobalpingError) as caught:
            gp._parse_playlist(body, "https://example.test/list.m3u")
        self.assertEqual(caught.exception.status, gp.FAIL)

    def test_rejects_encrypted_hls(self):
        body = MEDIA_PLAYLIST.replace(b"#EXTINF:10,", b'#EXT-X-KEY:METHOD=AES-128,URI="key"\n#EXTINF:10,')
        with self.assertRaises(gp.GlobalpingError) as caught:
            gp._parse_playlist(body, "https://example.test/index.m3u8")
        self.assertEqual(caught.exception.status, gp.FAIL)

    def test_rejects_fmp4_hls(self):
        body = MEDIA_PLAYLIST.replace(b"#EXTINF:10,", b'#EXT-X-MAP:URI="init.mp4"\n#EXTINF:10,')
        with self.assertRaises(gp.GlobalpingError) as caught:
            gp._parse_playlist(body, "https://example.test/index.m3u8")
        self.assertEqual(caught.exception.status, gp.FAIL)

    def test_rejects_malformed_extinf_pairing(self):
        body = b"#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXTINF:10,\n#EXTINF:10,\nseg.ts\n"
        with self.assertRaises(gp.GlobalpingError):
            gp._parse_playlist(body, "https://example.test/index.m3u8")


class TsRangeTests(unittest.TestCase):
    def test_accepts_complete_206_ts_range(self):
        result = finished_http(
            valid_ts(), 206,
            {"Content-Range": "bytes 0-1023/4096", "Content-Length": "1024"},
        )
        self.assertEqual(gp._range_ts_status(result)["status"], gp.PASS)

    def test_206_requires_valid_content_range(self):
        for header in (None, "bytes 10-1023/4096", "bytes 0-2048/4096"):
            with self.subTest(header=header):
                headers = {} if header is None else {"Content-Range": header}
                result = finished_http(valid_ts(), 206, headers)
                self.assertEqual(gp._range_ts_status(result)["status"], gp.FAIL)

    def test_206_short_body_is_incomplete(self):
        result = finished_http(
            valid_ts(700), 206,
            {"Content-Range": "bytes 0-699/4096", "Content-Length": "700"},
        )
        self.assertEqual(gp._range_ts_status(result)["status"], gp.INCOMPLETE)

    def test_206_content_range_body_length_mismatch_is_incomplete(self):
        result = finished_http(
            valid_ts(), 206,
            {"Content-Range": "bytes 0-1022/4096", "Content-Length": "1024"},
        )
        self.assertEqual(gp._range_ts_status(result)["status"], gp.INCOMPLETE)

    def test_truncated_ts_body_is_incomplete(self):
        result = finished_http(valid_ts(), 206, {"Content-Range": "bytes 0-1023/4096"}, truncated=True)
        self.assertEqual(gp._range_ts_status(result)["status"], gp.INCOMPLETE)

    def test_server_ignoring_range_only_passes_when_full_body_is_available(self):
        complete = finished_http(valid_ts(), 200, {"Content-Length": "1024"})
        partial = finished_http(valid_ts(), 200, {"Content-Length": "4096"})
        self.assertEqual(gp._range_ts_status(complete)["status"], gp.PASS)
        self.assertEqual(gp._range_ts_status(partial)["status"], gp.INCOMPLETE)

    def test_bad_ts_sync_is_fail(self):
        result = finished_http(b"A" * 1024, 206, {"Content-Range": "bytes 0-1023/4096"})
        self.assertEqual(gp._range_ts_status(result)["status"], gp.FAIL)


class StatusAndFlowTests(unittest.TestCase):
    def test_na_does_not_count_as_pass(self):
        checks = {
            "ipv4": gp._status(gp.PASS, "ok"),
            "http": gp._status(gp.PASS, "ok"),
            "hls": gp._status(gp.NA, "not applicable"),
            "ts": gp._status(gp.INCOMPLETE, "not checked"),
        }
        self.assertEqual(gp._overall_status(checks), gp.INCOMPLETE)

    def test_unknown_media_type_fails_without_api_calls(self):
        with patch.object(gp, "read_limits") as limits, patch.object(gp, "choose_probes") as probes:
            output = gp.check_candidates([{"name": "x", "media_type": "UNKNOWN", "normalized_url": "http://example.test/a"}], {"max_probes": 3})
        limits.assert_not_called()
        probes.assert_not_called()
        self.assertEqual(output[0]["globalping"]["status"], gp.FAIL)

    def test_direct_ts_marks_hls_na_but_requires_ts_pass(self):
        probe_key = gp._probe_key(PROBE)
        ts_result = finished_http(
            valid_ts(), 206,
            {"Content-Range": "bytes 0-1023/4096", "Content-Length": "1024"},
        )
        with patch.object(gp, "read_limits", return_value={"limit": 50, "remaining": 10, "reset": 1}), \
             patch.object(gp, "choose_probes", return_value=[{"country": "CN", "asn": 9808, "tags": ["eyeball-network"], "limit": 1}]), \
             patch.object(gp, "_stage", return_value=("m1", measurement(result_item(ts_result)))):
            output = gp.check_candidates([{"id": "c1", "media_type": "DIRECT_TS", "normalized_url": "http://example.test/live.ts"}], {"max_probes": 3})
        checks = output[0]["globalping"]["checks"]
        self.assertEqual(checks["hls"]["status"], gp.NA)
        self.assertEqual(checks["ts"]["status"], gp.PASS)
        self.assertEqual(output[0]["globalping"]["status"], gp.PASS)

    def test_hls_requires_main_media_and_ts_stages(self):
        main_result = finished_http(MASTER_PLAYLIST, 200)
        media_result = finished_http(MEDIA_PLAYLIST, 200)
        ts_result = finished_http(
            valid_ts(), 206,
            {"Content-Range": "bytes 0-1023/4096", "Content-Length": "1024"},
        )
        responses = [
            ("m1", measurement(result_item(main_result))),
            ("m2", measurement(result_item(media_result))),
            ("m3", measurement(result_item(ts_result))),
        ]
        with patch.object(gp, "read_limits", return_value={"limit": 50, "remaining": 20, "reset": 1}), \
             patch.object(gp, "choose_probes", return_value=[{"country": "CN", "asn": 9808, "tags": ["eyeball-network"], "limit": 1}]), \
             patch.object(gp, "_stage", side_effect=responses):
            output = gp.check_candidates([{"id": "c1", "media_type": "HLS", "normalized_url": "http://example.test/master.m3u8"}], {"max_probes": 3})
        result = output[0]["globalping"]
        self.assertEqual(result["status"], gp.PASS)
        self.assertEqual(set(result["stages"]), {"main_playlist", "media_playlist", "ts_range"})
        self.assertEqual(len(result["measurements"]), 3)

    def test_rate_limit_stops_later_candidate_measurements(self):
        calls = []
        def rate_limited(*args, **kwargs):
            calls.append(1)
            raise gp.GlobalpingError("HTTP 429", rate_limited=True)
        with patch.object(gp, "read_limits", return_value={"limit": 50, "remaining": 20, "reset": 1}), \
             patch.object(gp, "choose_probes", return_value=[{"country": "CN", "asn": 9808, "tags": ["eyeball-network"], "limit": 1}]), \
             patch.object(gp, "_stage", side_effect=rate_limited):
            output = gp.check_candidates([
                {"id": "c1", "media_type": "DIRECT_TS", "normalized_url": "http://example.test/a.ts"},
                {"id": "c2", "media_type": "DIRECT_TS", "normalized_url": "http://example.test/b.ts"},
            ], {"max_probes": 3})
        self.assertEqual(len(calls), 1)
        self.assertEqual(output[0]["globalping"]["status"], gp.INCOMPLETE)
        self.assertEqual(output[1]["globalping"]["status"], gp.INCOMPLETE)
        self.assertIn("停止", output[1]["globalping"]["error"])

    def test_halted_budget_prevents_new_measurement(self):
        with patch.object(gp, "_api_request") as request:
            with self.assertRaises(gp.GlobalpingError):
                gp._create_measurement(
                    "http://example.test/a.ts",
                    [{"country": "CN", "asn": 9808, "limit": 1}],
                    {"remaining": 10, "probe_count": 1, "halted": True},
                )
        request.assert_not_called()

    def test_insufficient_quota_stops_later_candidate_measurements(self):
        def quota_exhausted(url, locations, budget, headers=None):
            gp._create_measurement(
                url,
                locations,
                budget,
                headers,
            )

        candidates = [
            {
                "id": "c1",
                "media_type": "DIRECT_TS",
                "normalized_url": "http://example.test/a.ts",
            },
            {
                "id": "c2",
                "media_type": "DIRECT_TS",
                "normalized_url": "http://example.test/b.ts",
            },
        ]

        with patch.object(
            gp,
            "read_limits",
            return_value={"limit": 50, "remaining": 10, "reset": 1},
        ), patch.object(
            gp,
            "choose_probes",
            return_value=[
                {
                    "country": "CN",
                    "asn": 9808,
                    "tags": ["eyeball-network"],
                    "limit": 1,
                }
            ],
        ), patch.object(
            gp,
            "_remaining_budget",
            return_value=0,
        ) as remaining, patch.object(
            gp,
            "_stage",
            side_effect=quota_exhausted,
        ) as stage:
            output = gp.check_candidates(
                candidates,
                {"max_probes": 3},
            )

        self.assertEqual(stage.call_count, 1)
        self.assertEqual(remaining.call_count, 1)
        self.assertEqual(
            output[0]["globalping"]["status"],
            gp.INCOMPLETE,
        )
        self.assertEqual(
            output[1]["globalping"]["status"],
            gp.INCOMPLETE,
        )
        self.assertIn(
            "停止",
            output[1]["globalping"]["error"],
        )


    def test_shared_session_reuses_quota_and_probes(self):
        settings = {"max_probes": 3}
        candidates = [
            {
                "id": "c1",
                "media_type": "DIRECT_TS",
                "normalized_url": "http://example.test/a.ts",
            },
            {
                "id": "c2",
                "media_type": "DIRECT_TS",
                "normalized_url": "http://example.test/b.ts",
            },
        ]
        ts_result = finished_http(
            valid_ts(), 206,
            {
                "Content-Range": "bytes 0-1023/4096",
                "Content-Length": "1024",
            },
        )

        with patch.object(
            gp,
            "read_limits",
            return_value={"limit": 50, "remaining": 20, "reset": 1},
        ) as limits, patch.object(
            gp,
            "choose_probes",
            return_value=[
                {
                    "country": "CN",
                    "asn": 9808,
                    "tags": ["eyeball-network"],
                    "limit": 1,
                }
            ],
        ) as probes, patch.object(
            gp,
            "_stage",
            return_value=("m1", measurement(result_item(ts_result))),
        ) as stage:
            session = gp.create_session(settings)

            first = gp.check_candidates(
                [candidates[0]], settings, session=session
            )
            second = gp.check_candidates(
                [candidates[1]], settings, session=session
            )

        self.assertEqual(limits.call_count, 1)
        self.assertEqual(probes.call_count, 1)
        self.assertEqual(stage.call_count, 2)
        self.assertEqual(first[0]["globalping"]["status"], gp.PASS)
        self.assertEqual(second[0]["globalping"]["status"], gp.PASS)

    def test_shared_session_stops_after_rate_limit(self):
        settings = {"max_probes": 3}
        candidates = [
            {
                "id": "c1",
                "media_type": "DIRECT_TS",
                "normalized_url": "http://example.test/a.ts",
            },
            {
                "id": "c2",
                "media_type": "DIRECT_TS",
                "normalized_url": "http://example.test/b.ts",
            },
        ]

        with patch.object(
            gp,
            "read_limits",
            return_value={"limit": 50, "remaining": 20, "reset": 1},
        ), patch.object(
            gp,
            "choose_probes",
            return_value=[
                {
                    "country": "CN",
                    "asn": 9808,
                    "tags": ["eyeball-network"],
                    "limit": 1,
                }
            ],
        ):
            session = gp.create_session(settings)

        with patch.object(
            gp,
            "_stage",
            side_effect=gp.GlobalpingError(
                "HTTP 429", rate_limited=True
            ),
        ) as stage:
            first = gp.check_candidates(
                [candidates[0]], settings, session=session
            )
            second = gp.check_candidates(
                [candidates[1]], settings, session=session
            )

        self.assertEqual(stage.call_count, 1)
        self.assertTrue(session["budget"]["halted"])
        self.assertEqual(first[0]["globalping"]["status"], gp.INCOMPLETE)
        self.assertEqual(second[0]["globalping"]["status"], gp.INCOMPLETE)

    def test_session_initialization_error_is_preserved(self):
        settings = {"max_probes": 3}
        candidate = {
            "id": "c1",
            "media_type": "DIRECT_TS",
            "normalized_url": "http://example.test/a.ts",
        }

        with patch.object(
            gp,
            "read_limits",
            side_effect=gp.GlobalpingError("额度查询失败"),
        ), patch.object(gp, "choose_probes") as probes:
            session = gp.create_session(settings)

        with patch.object(gp, "_stage") as stage:
            output = gp.check_candidates(
                [candidate], settings, session=session
            )

        self.assertIn("额度查询失败", session["error"])
        self.assertEqual(output[0]["globalping"]["status"], gp.INCOMPLETE)
        probes.assert_not_called()
        stage.assert_not_called()


if __name__ == "__main__":
    unittest.main()
