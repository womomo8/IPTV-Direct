
"""IPTV-Direct globalping.py 专项测试。

仅使用模拟 API 响应和本地数据，不访问 Globalping 公网 API，
不创建真实测量，不消耗额度，不修改正式订阅。

运行方式：
    python -m unittest discover -s tests -p "test_globalping.py" -v
"""

import os
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


def result_item(result, probe=None):
    return {"probe": dict(probe or PROBE), "result": dict(result)}


def finished_http(body, status_code=200, headers=None, **extra):
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="replace")

    value = {
        "status": "finished",
        "resolvedAddress": "1.2.3.4",
        "statusCode": status_code,
        "headers": headers or {},
        "rawBody": body,
        "truncated": False,
    }
    value.update(extra)
    return value


def measurement(*items):
    return {"status": "finished", "results": list(items)}


def one_probe_location():
    return [{
        "country": "CN",
        "asn": 9808,
        "tags": ["eyeball-network"],
        "limit": 1,
    }]


def mocked_limits(remaining=50):
    return {
        "limit": 500,
        "remaining": remaining,
        "reset": 12345,
    }


class ApiRequestTests(unittest.TestCase):
    def test_json_array_response_is_supported(self):
        response = Mock()
        response.status = 200
        response.headers = {"Content-Type": "application/json"}
        response.read.return_value = b'[{"id":"p1"}]'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)

        with patch.object(gp.urllib.request, "urlopen", return_value=response):
            status, data, headers = gp._api_request(
                "https://example.test/probes"
            )

        self.assertEqual(status, 200)
        self.assertEqual(data, [{"id": "p1"}])

    def test_token_is_sent_as_bearer_authorization(self):
        response = Mock()
        response.status = 200
        response.headers = {"Content-Type": "application/json"}
        response.read.return_value = b'{}'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)

        with patch.dict(os.environ, {"GLOBALPING_API_TOKEN": "test-secret"}):
            with patch.object(
                gp.urllib.request,
                "urlopen",
                return_value=response,
            ) as request:
                gp._api_request("https://example.test/limits")

        sent_request = request.call_args.args[0]
        self.assertEqual(
            sent_request.get_header("Authorization"),
            "Bearer test-secret",
        )

    def test_missing_token_does_not_add_authorization_header(self):
        response = Mock()
        response.status = 200
        response.headers = {"Content-Type": "application/json"}
        response.read.return_value = b'{}'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)

        with patch.dict(os.environ, {}, clear=True):
            with patch.object(
                gp.urllib.request,
                "urlopen",
                return_value=response,
            ) as request:
                gp._api_request("https://example.test/limits")

        sent_request = request.call_args.args[0]
        self.assertIsNone(sent_request.get_header("Authorization"))

    def test_304_is_normal_response(self):
        error = urllib.error.HTTPError(
            "https://example.test/measurements/id",
            304,
            "Not Modified",
            {"ETag": '"abc"'},
            None,
        )

        with patch.object(gp.urllib.request, "urlopen", side_effect=error):
            status, data, headers = gp._api_request(
                "https://example.test/measurements/id"
            )

        self.assertEqual(status, 304)
        self.assertIsNone(data)
        self.assertEqual(headers.get("etag"), '"abc"')

    def test_429_is_marked_rate_limited(self):
        error = urllib.error.HTTPError(
            "https://example.test/measurements",
            429,
            "Too Many Requests",
            {"Retry-After": "10"},
            None,
        )

        with patch.object(gp.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(gp.GlobalpingError) as caught:
                gp._api_request(
                    "https://example.test/measurements",
                    method="POST",
                    payload={},
                )

        self.assertTrue(caught.exception.rate_limited)
        self.assertIn("429", str(caught.exception))

    def test_read_limits_parses_quota(self):
        payload = {
            "rateLimit": {
                "measurements": {
                    "create": {
                        "limit": 500,
                        "remaining": 7,
                        "reset": 12345,
                    }
                }
            }
        }

        with patch.object(
            gp,
            "_api_request",
            return_value=(200, payload, {}),
        ):
            self.assertEqual(
                gp.read_limits(),
                {"limit": 500, "remaining": 7, "reset": 12345},
            )

    def test_read_limits_rejects_unknown_shape(self):
        with patch.object(
            gp,
            "_api_request",
            return_value=(200, [], {}),
        ):
            with self.assertRaises(gp.GlobalpingError):
                gp.read_limits()


class ProbeSelectionTests(unittest.TestCase):
    def test_selects_mainland_eyeball_probes_mobile_first(self):
        probes = [
            {
                "tags": ["eyeball-network"],
                "location": {
                    "country": "CN",
                    "asn": 9808,
                    "network": "China Mobile",
                },
            },
            {
                "tags": ["eyeball-network"],
                "location": {
                    "country": "CN",
                    "asn": 4134,
                    "network": "China Telecom",
                },
            },
            {
                "tags": ["eyeball-network"],
                "location": {
                    "country": "CN",
                    "asn": 4837,
                    "network": "China Unicom",
                },
            },
            {
                "tags": ["eyeball-network"],
                "location": {
                    "country": "US",
                    "asn": 9808,
                    "network": "China Mobile",
                },
            },
            {
                "tags": ["hosting"],
                "location": {
                    "country": "CN",
                    "asn": 9808,
                    "network": "China Mobile",
                },
            },
        ]

        with patch.object(
            gp,
            "_api_request",
            return_value=(200, probes, {}),
        ):
            locations = gp.choose_probes(3)

        self.assertEqual(sum(item["limit"] for item in locations), 3)
        self.assertLessEqual(sum(item["limit"] for item in locations), 3)
        self.assertEqual(locations[0]["asn"], 9808)
        self.assertTrue(all(item["country"] == "CN" for item in locations))
        self.assertTrue(
            all(item["tags"] == ["eyeball-network"] for item in locations)
        )

    def test_rejects_invalid_probe_limit(self):
        for value in (0, -1, True, "3"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    gp.choose_probes(value)

    def test_rejects_non_array_probe_response(self):
        with patch.object(
            gp,
            "_api_request",
            return_value=(200, {"data": []}, {}),
        ):
            with self.assertRaises(gp.GlobalpingError):
                gp.choose_probes(3)

    def test_no_eligible_probe_is_rejected(self):
        with patch.object(
            gp,
            "_api_request",
            return_value=(
                200,
                [{
                    "tags": ["eyeball-network"],
                    "location": {"country": "US", "asn": 9808},
                }],
                {},
            ),
        ):
            with self.assertRaises(gp.GlobalpingError):
                gp.choose_probes(3)


class UrlAndBudgetTests(unittest.TestCase):
    def test_split_url_preserves_path_query_and_port(self):
        parts = gp._split_url(
            "https://example.test:8443/live/index.m3u8?token=x"
        )

        self.assertEqual(parts["target"], "example.test")
        self.assertEqual(parts["protocol"], "HTTPS")
        self.assertEqual(parts["port"], 8443)
        self.assertEqual(parts["path"], "/live/index.m3u8")
        self.assertEqual(parts["query"], "token=x")

    def test_ipv4_literal_omits_ip_version(self):
        budget = {"remaining": 10, "probe_count": 1}
        locations = one_probe_location()

        with patch.object(
            gp,
            "_api_request",
            return_value=(202, {"id": "m1", "probesCount": 1}, {}),
        ) as request:
            measurement_id = gp._create_measurement(
                "http://1.2.3.4/live.ts",
                locations,
                budget,
            )

        self.assertEqual(measurement_id, "m1")
        payload = request.call_args.kwargs["payload"]
        self.assertNotIn("ipVersion", payload["measurementOptions"])
        self.assertEqual(budget["remaining"], 9)

    def test_domain_measurement_keeps_ipv4_version(self):
        budget = {"remaining": 10, "probe_count": 1}
        locations = one_probe_location()

        with patch.object(
            gp,
            "_api_request",
            return_value=(202, {"id": "m1", "probesCount": 1}, {}),
        ) as request:
            gp._create_measurement(
                "http://example.test/live.ts",
                locations,
                budget,
            )

        payload = request.call_args.kwargs["payload"]
        self.assertEqual(payload["measurementOptions"]["ipVersion"], 4)

    def test_create_measurement_does_not_query_limits_again(self):
        budget = {"remaining": 10, "probe_count": 1}
        locations = one_probe_location()

        with patch.object(
            gp,
            "read_limits",
            side_effect=AssertionError("不应重复查询额度"),
        ):
            with patch.object(
                gp,
                "_api_request",
                return_value=(202, {"id": "m1", "probesCount": 1}, {}),
            ):
                result = gp._create_measurement(
                    "http://example.test/live.ts",
                    locations,
                    budget,
                )

        self.assertEqual(result, "m1")
        self.assertEqual(budget["remaining"], 9)

    def test_insufficient_budget_does_not_create_measurement(self):
        budget = {"remaining": 0, "probe_count": 1}

        with patch.object(gp, "_api_request") as request:
            with self.assertRaises(gp.GlobalpingError):
                gp._create_measurement(
                    "http://example.test/live.ts",
                    one_probe_location(),
                    budget,
                )

        request.assert_not_called()

    def test_429_halts_budget(self):
        budget = {"remaining": 10, "probe_count": 1}
        error = gp.GlobalpingError("HTTP 429", rate_limited=True)

        with patch.object(gp, "_api_request", side_effect=error):
            with self.assertRaises(gp.GlobalpingError):
                gp._create_measurement(
                    "http://example.test/live.ts",
                    one_probe_location(),
                    budget,
                )

        self.assertTrue(budget["halted"])

    def test_split_url_rejects_ipv6_literal(self):
        with self.assertRaises(gp.GlobalpingError):
            gp._split_url("http://[2001:db8::1]/live.ts")

    def test_split_url_rejects_credentials_and_unsupported_scheme(self):
        for url in (
            "http://user:pass@example.test/a",
            "ftp://example.test/a",
        ):
            with self.subTest(url=url):
                with self.assertRaises(gp.GlobalpingError):
                    gp._split_url(url)

    def test_halted_budget_prevents_measurement_creation(self):
        with patch.object(gp, "_api_request") as request:
            with self.assertRaises(gp.GlobalpingError):
                gp._create_measurement(
                    "http://example.test/a.ts",
                    one_probe_location(),
                    {
                        "remaining": 10,
                        "probe_count": 1,
                        "halted": True,
                    },
                )

        request.assert_not_called()


class BodyAndPlaylistTests(unittest.TestCase):
    def test_raw_body_rejects_missing_body(self):
        self.assertIsNone(gp._raw_body_bytes({}))

    def test_raw_body_rejects_truncated_body(self):
        self.assertIsNone(
            gp._raw_body_bytes({
                "rawBody": "abc",
                "truncated": True,
            })
        )

    def test_raw_body_rejects_lossy_utf8_replacement_character(self):
        self.assertIsNone(
            gp._raw_body_bytes({
                "rawBody": "abc\ufffdef",
                "truncated": False,
            })
        )

    def test_raw_body_accepts_complete_utf8_playlist_text(self):
        body = "#EXTM3U\n#EXT-X-VERSION:3\n"
        result = gp._raw_body_bytes({
            "rawBody": body,
            "truncated": False,
        })

        self.assertEqual(result, body.encode("utf-8"))

    def test_raw_body_rejects_body_over_limit(self):
        self.assertIsNone(
            gp._raw_body_bytes({
                "rawBody": "A" * (gp.MAX_BODY_BYTES + 1),
                "truncated": False,
            })
        )

    def test_parses_media_playlist_and_resolves_segment(self):
        parsed = gp._parse_playlist(
            MEDIA_PLAYLIST,
            "https://example.test/live/index.m3u8",
        )

        self.assertEqual(parsed["kind"], "media")
        self.assertEqual(
            parsed["segments"],
            ["https://example.test/live/segment.ts"],
        )

    def test_parses_master_playlist_and_resolves_variant(self):
        parsed = gp._parse_playlist(
            MASTER_PLAYLIST,
            "https://example.test/live/master.m3u8",
        )

        self.assertEqual(parsed["kind"], "master")
        self.assertEqual(
            parsed["child_url"],
            "https://example.test/live/variant/index.m3u8",
        )

    def test_rejects_channel_subscription_m3u(self):
        body = (
            b"#EXTM3U\n"
            b"#EXTINF:-1,CCTV-5\n"
            b"http://example.test/live.ts\n"
        )

        with self.assertRaises(gp.GlobalpingError) as caught:
            gp._parse_playlist(body, "https://example.test/list.m3u")

        self.assertEqual(caught.exception.status, gp.FAIL)

    def test_rejects_encrypted_hls(self):
        body = MEDIA_PLAYLIST.replace(
            b"#EXTINF:10,",
            b'#EXT-X-KEY:METHOD=AES-128,URI="key"\n#EXTINF:10,',
        )

        with self.assertRaises(gp.GlobalpingError) as caught:
            gp._parse_playlist(body, "https://example.test/index.m3u8")

        self.assertEqual(caught.exception.status, gp.FAIL)

    def test_rejects_fmp4_hls(self):
        body = MEDIA_PLAYLIST.replace(
            b"#EXTINF:10,",
            b'#EXT-X-MAP:URI="init.mp4"\n#EXTINF:10,',
        )

        with self.assertRaises(gp.GlobalpingError) as caught:
            gp._parse_playlist(body, "https://example.test/index.m3u8")

        self.assertEqual(caught.exception.status, gp.FAIL)

    def test_rejects_malformed_extinf_pairing(self):
        body = (
            b"#EXTM3U\n"
            b"#EXT-X-TARGETDURATION:10\n"
            b"#EXTINF:10,\n"
            b"#EXTINF:10,\n"
            b"seg.ts\n"
        )

        with self.assertRaises(gp.GlobalpingError):
            gp._parse_playlist(body, "https://example.test/index.m3u8")


class HttpStatusTests(unittest.TestCase):
    def test_http_200_is_success(self):
        result = {
            "status": "finished",
            "resolvedAddress": "1.2.3.4",
            "statusCode": 200,
        }
        self.assertTrue(gp._http_ok(result))
        self.assertTrue(gp._ipv4_ok(result))

    def test_http_206_is_success_for_range_reachability(self):
        result = {
            "status": "finished",
            "resolvedAddress": "1.2.3.4",
            "statusCode": 206,
        }
        self.assertTrue(gp._http_ok(result))

    def test_http_error_is_not_success(self):
        for code in (301, 403, 404, 500):
            with self.subTest(code=code):
                result = {
                    "status": "finished",
                    "resolvedAddress": "1.2.3.4",
                    "statusCode": code,
                }
                self.assertFalse(gp._http_ok(result))

    def test_ipv6_resolution_does_not_pass_ipv4(self):
        result = {
            "status": "finished",
            "resolvedAddress": "2001:db8::1",
            "statusCode": 200,
        }
        self.assertFalse(gp._ipv4_ok(result))

    def test_missing_or_invalid_address_does_not_pass_ipv4(self):
        for address in (None, "", "not-an-ip"):
            with self.subTest(address=address):
                result = {
                    "status": "finished",
                    "resolvedAddress": address,
                    "statusCode": 200,
                }
                self.assertFalse(gp._ipv4_ok(result))

    def test_http_aggregate_accepts_any_successful_ipv4_probe(self):
        results = {
            ("a",): result_item({
                "status": "finished",
                "resolvedAddress": "1.2.3.4",
                "statusCode": 403,
            }),
            ("b",): result_item({
                "status": "finished",
                "resolvedAddress": "2.3.4.5",
                "statusCode": 206,
            }),
        }

        self.assertEqual(gp._aggregate_http(results)["status"], gp.PASS)

    def test_http_aggregate_rejects_all_http_errors(self):
        results = {
            ("a",): result_item({
                "status": "finished",
                "resolvedAddress": "1.2.3.4",
                "statusCode": 403,
            }),
        }

        self.assertEqual(gp._aggregate_http(results)["status"], gp.FAIL)


class DirectTsFlowTests(unittest.TestCase):
    def test_direct_ts_uses_one_range_http_measurement_only(self):
        response = finished_http(
            "not binary data; Globalping cannot validate TS bytes",
            206,
            {"Content-Range": "bytes 0-0/4096"},
        )

        with patch.object(
            gp,
            "_stage",
            return_value=("m1", measurement(result_item(response))),
        ) as stage:
            result = gp._run_direct_ts(
                {
                    "media_type": "DIRECT_TS",
                    "normalized_url": "http://example.test/live.ts",
                },
                one_probe_location(),
                {"remaining": 10, "probe_count": 1, "halted": False},
            )

        self.assertEqual(stage.call_count, 1)
        self.assertEqual(
            stage.call_args.kwargs["headers"],
            {"Range": "bytes=0-0"},
        )
        self.assertEqual(result["measurements"], ["m1"])
        self.assertEqual(result["checks"]["ipv4"]["status"], gp.PASS)
        self.assertEqual(result["checks"]["http"]["status"], gp.PASS)
        self.assertEqual(result["checks"]["hls"]["status"], gp.NA)
        self.assertEqual(result["checks"]["ts"]["status"], gp.INCOMPLETE)
        self.assertEqual(
            result["stages"]["direct_ts_http"]["status"],
            gp.PASS,
        )
        self.assertEqual(
            result["stages"]["ts_binary"]["status"],
            gp.INCOMPLETE,
        )
        self.assertIsNone(
            result["stages"]["ts_binary"]["measurement_id"]
        )

    def test_direct_ts_http_200_is_reachability_success(self):
        response = finished_http("ok", 200)

        with patch.object(
            gp,
            "_stage",
            return_value=("m1", measurement(result_item(response))),
        ):
            result = gp._run_direct_ts(
                {
                    "media_type": "DIRECT_TS",
                    "normalized_url": "http://example.test/live.ts",
                },
                one_probe_location(),
                {"remaining": 10, "probe_count": 1, "halted": False},
            )

        self.assertEqual(result["checks"]["http"]["status"], gp.PASS)
        self.assertEqual(result["checks"]["ts"]["status"], gp.INCOMPLETE)

    def test_direct_ts_http_403_does_not_pass(self):
        response = finished_http("forbidden", 403)

        with patch.object(
            gp,
            "_stage",
            return_value=("m1", measurement(result_item(response))),
        ):
            result = gp._run_direct_ts(
                {
                    "media_type": "DIRECT_TS",
                    "normalized_url": "http://example.test/live.ts",
                },
                one_probe_location(),
                {"remaining": 10, "probe_count": 1, "halted": False},
            )

        self.assertEqual(result["checks"]["http"]["status"], gp.FAIL)
        self.assertNotEqual(result["checks"]["ts"]["status"], gp.PASS)


class HlsFlowTests(unittest.TestCase):
    def test_master_hls_uses_only_main_and_media_playlist_stages(self):
        main_response = finished_http(MASTER_PLAYLIST, 200)
        media_response = finished_http(MEDIA_PLAYLIST, 200)

        responses = [
            ("m1", measurement(result_item(main_response))),
            ("m2", measurement(result_item(media_response))),
        ]

        with patch.object(
            gp,
            "_stage",
            side_effect=responses,
        ) as stage:
            result = gp._run_hls(
                {
                    "media_type": "HLS",
                    "normalized_url": "http://example.test/master.m3u8",
                },
                one_probe_location(),
                {"remaining": 10, "probe_count": 1, "halted": False},
            )

        self.assertEqual(stage.call_count, 2)
        self.assertEqual(result["measurements"], ["m1", "m2"])
        self.assertEqual(
            set(result["stages"]),
            {"main_playlist", "media_playlist", "ts_binary"},
        )
        self.assertEqual(result["checks"]["hls"]["status"], gp.PASS)
        self.assertEqual(result["checks"]["ts"]["status"], gp.INCOMPLETE)
        self.assertEqual(result["stages"]["ts_binary"]["status"], gp.INCOMPLETE)
        self.assertIsNone(result["stages"]["ts_binary"]["measurement_id"])

    def test_media_playlist_hls_needs_only_one_measurement(self):
        response = finished_http(MEDIA_PLAYLIST, 200)

        with patch.object(
            gp,
            "_stage",
            return_value=("m1", measurement(result_item(response))),
        ) as stage:
            result = gp._run_hls(
                {
                    "media_type": "HLS",
                    "normalized_url": "http://example.test/index.m3u8",
                },
                one_probe_location(),
                {"remaining": 10, "probe_count": 1, "halted": False},
            )

        self.assertEqual(stage.call_count, 1)
        self.assertEqual(result["measurements"], ["m1"])
        self.assertEqual(result["checks"]["hls"]["status"], gp.PASS)
        self.assertEqual(result["checks"]["ts"]["status"], gp.INCOMPLETE)

    def test_hls_does_not_create_third_ts_measurement(self):
        responses = [
            ("m1", measurement(result_item(
                finished_http(MASTER_PLAYLIST, 200)
            ))),
            ("m2", measurement(result_item(
                finished_http(MEDIA_PLAYLIST, 200)
            ))),
        ]

        with patch.object(gp, "_stage", side_effect=responses) as stage:
            gp._run_hls(
                {
                    "media_type": "HLS",
                    "normalized_url": "http://example.test/master.m3u8",
                },
                one_probe_location(),
                {"remaining": 10, "probe_count": 1, "halted": False},
            )

        self.assertEqual(stage.call_count, 2)

    def test_hls_invalid_or_truncated_playlist_does_not_pass(self):
        response = finished_http(
            MEDIA_PLAYLIST,
            200,
            truncated=True,
        )

        with patch.object(
            gp,
            "_stage",
            return_value=("m1", measurement(result_item(response))),
        ):
            result = gp._run_hls(
                {
                    "media_type": "HLS",
                    "normalized_url": "http://example.test/index.m3u8",
                },
                one_probe_location(),
                {"remaining": 10, "probe_count": 1, "halted": False},
            )

        self.assertNotEqual(result["checks"]["hls"]["status"], gp.PASS)

    def test_hls_master_requires_same_probe_for_media_playlist(self):
        other_probe = dict(PROBE)
        other_probe["city"] = "Fuzhou"

        responses = [
            ("m1", measurement(result_item(
                finished_http(MASTER_PLAYLIST, 200),
                PROBE,
            ))),
            ("m2", measurement(result_item(
                finished_http(MEDIA_PLAYLIST, 200),
                other_probe,
            ))),
        ]

        with patch.object(gp, "_stage", side_effect=responses):
            result = gp._run_hls(
                {
                    "media_type": "HLS",
                    "normalized_url": "http://example.test/master.m3u8",
                },
                one_probe_location(),
                {"remaining": 10, "probe_count": 1, "halted": False},
            )

        self.assertNotEqual(result["checks"]["hls"]["status"], gp.PASS)


class StatusAndFlowTests(unittest.TestCase):
    def test_na_does_not_count_as_pass(self):
        checks = {
            "ipv4": gp._status(gp.PASS, "ok"),
            "http": gp._status(gp.PASS, "ok"),
            "hls": gp._status(gp.NA, "not applicable"),
            "ts": gp._status(gp.INCOMPLETE, "not checked"),
        }

        self.assertEqual(gp._overall_status(checks), gp.INCOMPLETE)

    def test_ts_incomplete_prevents_overall_pass(self):
        checks = {
            "ipv4": gp._status(gp.PASS, "ok"),
            "http": gp._status(gp.PASS, "ok"),
            "hls": gp._status(gp.PASS, "ok"),
            "ts": gp._status(gp.INCOMPLETE, "binary not validated"),
        }

        self.assertEqual(gp._overall_status(checks), gp.INCOMPLETE)

    def test_any_required_failure_makes_overall_fail(self):
        checks = {
            "ipv4": gp._status(gp.PASS, "ok"),
            "http": gp._status(gp.FAIL, "failed"),
            "hls": gp._status(gp.PASS, "ok"),
            "ts": gp._status(gp.INCOMPLETE, "not checked"),
        }

        self.assertEqual(gp._overall_status(checks), gp.FAIL)

    def test_unknown_media_type_fails_without_api_calls(self):
        with patch.object(gp, "read_limits") as limits:
            with patch.object(gp, "choose_probes") as probes:
                output = gp.check_candidates(
                    [{
                        "name": "x",
                        "media_type": "UNKNOWN",
                        "normalized_url": "http://example.test/a",
                    }],
                    {"max_probes": 3},
                )

        limits.assert_not_called()
        probes.assert_not_called()
        self.assertEqual(output[0]["globalping"]["status"], gp.FAIL)

    def test_direct_ts_final_result_is_incomplete_not_pass(self):
        response = finished_http("not a TS binary validation", 206)

        with patch.object(gp, "read_limits", return_value=mocked_limits(10)):
            with patch.object(
                gp,
                "choose_probes",
                return_value=one_probe_location(),
            ):
                with patch.object(
                    gp,
                    "_stage",
                    return_value=("m1", measurement(result_item(response))),
                ):
                    output = gp.check_candidates(
                        [{
                            "id": "c1",
                            "media_type": "DIRECT_TS",
                            "normalized_url": "http://example.test/live.ts",
                        }],
                        {"max_probes": 3},
                    )

        checks = output[0]["globalping"]["checks"]
        self.assertEqual(checks["hls"]["status"], gp.NA)
        self.assertEqual(checks["ts"]["status"], gp.INCOMPLETE)
        self.assertEqual(output[0]["globalping"]["status"], gp.INCOMPLETE)

    def test_hls_final_result_is_incomplete_when_ts_is_not_remote_validated(self):
        responses = [
            ("m1", measurement(result_item(
                finished_http(MASTER_PLAYLIST, 200)
            ))),
            ("m2", measurement(result_item(
                finished_http(MEDIA_PLAYLIST, 200)
            ))),
        ]

        with patch.object(gp, "read_limits", return_value=mocked_limits(10)):
            with patch.object(
                gp,
                "choose_probes",
                return_value=one_probe_location(),
            ):
                with patch.object(gp, "_stage", side_effect=responses):
                    output = gp.check_candidates(
                        [{
                            "id": "c1",
                            "media_type": "HLS",
                            "normalized_url": "http://example.test/master.m3u8",
                        }],
                        {"max_probes": 3},
                    )

        result = output[0]["globalping"]
        self.assertEqual(result["checks"]["hls"]["status"], gp.PASS)
        self.assertEqual(result["checks"]["ts"]["status"], gp.INCOMPLETE)
        self.assertEqual(result["status"], gp.INCOMPLETE)
        self.assertEqual(len(result["measurements"]), 2)

    def test_hls_requires_two_stage_budget_not_three(self):
        calls = []

        def fake_stage(url, locations, budget, headers=None):
            calls.append((url, headers))
            return (
                f"m{len(calls)}",
                measurement(result_item(
                    finished_http(
                        MASTER_PLAYLIST if len(calls) == 1 else MEDIA_PLAYLIST,
                        200,
                    )
                )),
            )

        with patch.object(gp, "read_limits", return_value=mocked_limits(2)):
            with patch.object(
                gp,
                "choose_probes",
                return_value=one_probe_location(),
            ):
                with patch.object(gp, "_stage", side_effect=fake_stage):
                    output = gp.check_candidates(
                        [{
                            "id": "hls",
                            "media_type": "HLS",
                            "normalized_url": "http://example.test/master.m3u8",
                        }],
                        {"max_probes": 3},
                    )

        self.assertEqual(len(calls), 2)
        self.assertEqual(output[0]["globalping"]["checks"]["hls"]["status"], gp.PASS)
        self.assertEqual(output[0]["globalping"]["checks"]["ts"]["status"], gp.INCOMPLETE)

    def test_hls_is_skipped_when_two_stage_budget_is_unavailable(self):
        with patch.object(gp, "read_limits", return_value=mocked_limits(1)):
            with patch.object(
                gp,
                "choose_probes",
                return_value=one_probe_location(),
            ):
                with patch.object(gp, "_stage") as stage:
                    output = gp.check_candidates(
                        [{
                            "id": "hls",
                            "media_type": "HLS",
                            "normalized_url": "http://example.test/master.m3u8",
                        }],
                        {"max_probes": 3},
                    )

        stage.assert_not_called()
        self.assertEqual(
            output[0]["globalping"]["status"],
            gp.INCOMPLETE,
        )
        self.assertIn("额度不足", output[0]["globalping"]["error"])

    def test_rate_limit_stops_later_candidate_measurements(self):
        calls = []

        def rate_limited(*args, **kwargs):
            calls.append(1)
            raise gp.GlobalpingError("HTTP 429", rate_limited=True)

        with patch.object(gp, "read_limits", return_value=mocked_limits(20)):
            with patch.object(
                gp,
                "choose_probes",
                return_value=one_probe_location(),
            ):
                with patch.object(gp, "_stage", side_effect=rate_limited):
                    output = gp.check_candidates(
                        [
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
                        ],
                        {"max_probes": 3},
                    )

        self.assertEqual(len(calls), 1)
        self.assertEqual(output[0]["globalping"]["status"], gp.INCOMPLETE)
        self.assertEqual(output[1]["globalping"]["status"], gp.INCOMPLETE)
        self.assertIn("停止", output[1]["globalping"]["error"])


if __name__ == "__main__":
    unittest.main()
