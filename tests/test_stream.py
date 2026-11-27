
import json
import http.server
import threading
import time
import shutil
import tempfile
import subprocess
import unittest
from unittest.mock import patch

import stream

class StreamTimeoutTests(unittest.TestCase):

    def test_timeout_configuration_matches_measured_limits(self):
        self.assertEqual(stream.PROBE_TIMEOUT, 20)
        self.assertEqual(stream.PROBE_RW_TIMEOUT, 15_000_000)
        self.assertEqual(stream.DECODE_TIMEOUT, 20)
        self.assertEqual(stream.DECODE_RW_TIMEOUT, 20_000_000)
    
    def test_ffprobe_timeout_reason_is_preserved(self):
        with (
            patch(
                "stream.shutil.which",
                return_value="/usr/bin/ffmpeg",
            ),
            patch(
                "stream.subprocess.run",
                side_effect=subprocess.TimeoutExpired(
                    cmd="ffprobe",
                    timeout=stream.PROBE_TIMEOUT,
                ),
            ),
        ):
            result = stream.check_stream(
                "https://example.com/live.ts"
            )

        self.assertEqual(result["status"], stream.INCOMPLETE)
        self.assertIn(
            f"ffprobe执行超时（限制 {stream.PROBE_TIMEOUT} 秒）",
            result["reason"],
        )

    def test_ffmpeg_timeout_reason_is_preserved(self):
        metadata = {
            "codec": "h264",
            "width": 1920,
            "height": 1080,
            "field_order": "progressive",
            "fps": 25.0,
            "scan_type": "progressive",
        }

        with (
            patch(
                "stream.shutil.which",
                return_value="/usr/bin/ffmpeg",
            ),
            patch(
                "stream._probe",
                return_value=metadata,
            ),
            patch(
                "stream.subprocess.run",
                side_effect=subprocess.TimeoutExpired(
                    cmd="ffmpeg",
                    timeout=stream.DECODE_TIMEOUT,
                ),
            ),
        ):
            result = stream.check_stream(
                "https://example.com/live.ts"
            )

        self.assertEqual(result["status"], stream.INCOMPLETE)
        self.assertIn(
            f"ffmpeg执行超时（限制 {stream.DECODE_TIMEOUT} 秒）",
            result["reason"],
        )

class StreamQualityTests(unittest.TestCase):
    
    def metadata(self, **overrides):
        result = {
            "codec": "h264",
            "width": 1920,
            "height": 1080,
            "field_order": "progressive",
            "fps": 25.0,
            "scan_type": "progressive",
        }
        result.update(overrides)
        return result

    def check(self, metadata):
        with (
            patch("stream.shutil.which", return_value="/usr/bin/ffmpeg"),
            patch("stream._probe", return_value=metadata),
            patch("stream._decode") as decode,
        ):
            result = stream.check_stream(
                "https://example.com/live.ts"
            )
        return result, decode

    def test_h264_1080p_meeting_all_thresholds_passes(self):
        result, decode = self.check(self.metadata())

        self.assertEqual(result["status"], stream.PASS)
        self.assertEqual(result["width"], 1920)
        self.assertEqual(result["height"], 1080)
        self.assertEqual(result["fps"], 25.0)
        decode.assert_called_once()
        
    def test_hevc_1080p_passes(self):
        result, decode = self.check(
            self.metadata(codec="hevc")
        )
        
        self.assertEqual(result["status"], stream.PASS)
        decode.assert_called_once()

    def test_resolution_width_below_threshold_fails(self):
        result, decode = self.check(
            self.metadata(width=1919)
        )

        self.assertEqual(result["status"], stream.FAIL)
        decode.assert_not_called()

    def test_resolution_height_below_threshold_fails(self):
        result, decode = self.check(
            self.metadata(height=1079)
        )

        self.assertEqual(result["status"], stream.FAIL)
        decode.assert_not_called()

    def test_unsupported_codec_fails(self):
        result, decode = self.check(
            self.metadata(codec="mpeg2video")
        )

        self.assertEqual(result["status"], stream.FAIL)
        decode.assert_not_called()

    def test_fps_below_23_fails(self):
        result, decode = self.check(
            self.metadata(fps=22.99)
        )

        self.assertEqual(result["status"], stream.FAIL)
        decode.assert_not_called()

    def test_interlaced_video_is_not_rejected_by_field_order(self):
        result, decode = self.check(
            self.metadata(field_order="tt")
        )

        self.assertEqual(result["status"], stream.PASS)
        decode.assert_called_once()

    def test_missing_ffmpeg_returns_incomplete(self):
        with patch("stream.shutil.which", return_value=None):
            result = stream.check_stream(
                "https://example.com/live.ts"
            )

        self.assertEqual(result["status"], stream.INCOMPLETE)

    @patch("stream.shutil.which", return_value="/usr/bin/ffmpeg")
    @patch("stream._probe")
    def test_probe_failure_does_not_pass(self, probe, which):
        probe.side_effect = stream.StreamQualityError(
            "无法确认真实视频分辨率"
        )

        result = stream.check_stream(
            "https://example.com/live.ts"
        )

        self.assertEqual(result["status"], stream.INCOMPLETE)

    def test_unexpected_probe_exception_returns_incomplete(self):
        with (
            patch("stream.shutil.which", return_value="/usr/bin/ffmpeg"),
            patch(
                "stream._probe",
                side_effect=RuntimeError("unexpected probe error"),
            ),
        ):
            result = stream.check_stream(
                "https://example.com/live.ts"
            )

        self.assertEqual(result["status"], stream.INCOMPLETE)
        self.assertIn("RuntimeError", result["reason"])
    
    @patch("stream.shutil.which", return_value="/usr/bin/ffmpeg")
    @patch("stream._probe")
    @patch("stream._decode")
    def test_explicit_decode_failure_is_fail(
        self, decode, probe, which
    ):
        probe.return_value = self.metadata()
        decode.side_effect = stream.StreamDecodeError(
            "FFmpeg 确认视频解码失败：non-existing PPS 0 referenced"
        )

        result = stream.check_stream(
            "https://example.com/live.ts"
        )

        self.assertEqual(result["status"], stream.FAIL)
        self.assertIn("确认视频解码失败", result["reason"])
        decode.assert_called_once()

    @patch("stream.shutil.which", return_value="/usr/bin/ffmpeg")
    @patch("stream._probe")
    @patch("stream._decode")
    def test_ffmpeg_non_decode_error_is_incomplete(
        self, decode, probe, which
    ):
        probe.return_value = self.metadata()
        decode.side_effect = stream.StreamQualityError(
            "FFmpeg 执行失败，无法确认是否为解码错误："
            "Connection timed out"
        )

        result = stream.check_stream(
            "https://example.com/live.ts"
        )

        self.assertEqual(result["status"], stream.INCOMPLETE)
        decode.assert_called_once()

    def test_invalid_url_fails(self):
        result = stream.check_stream("not-a-valid-url")

        self.assertEqual(result["status"], stream.FAIL)

    @patch("stream.check_stream")
    def test_check_candidates_preserves_candidate(self, check):
        check.return_value = {
            "status": stream.PASS,
            "width": 1920,
            "height": 1080,
        }

        candidate = {
            "channel": "CCTV-1",
            "normalized_url": "https://example.com/live.ts",
        }

        results = stream.check_candidates([candidate])

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["channel"], "CCTV-1")
        self.assertEqual(
            results[0]["stream_quality"]["status"],
            stream.PASS,
        )
        check.assert_called_once()
        self.assertEqual(
            check.call_args.args[0],
            "https://example.com/live.ts",
        )
        self.assertEqual(len(check.call_args.args), 3)

    def test_check_candidates_limits_concurrency_and_isolates_errors(self):
        lock = threading.Lock()
        calls = {}
        active_probe = 0
        active_decode = 0
        max_probe = 0
        max_decode = 0

        def fake_check(url, probe_semaphore, decode_semaphore):
            nonlocal active_probe, active_decode, max_probe, max_decode
            with lock:
                calls[url] = calls.get(url, 0) + 1

            with probe_semaphore:
                with lock:
                    active_probe += 1
                    max_probe = max(max_probe, active_probe)
                time.sleep(0.02)
                with lock:
                    active_probe -= 1

            with decode_semaphore:
                with lock:
                    active_decode += 1
                    max_decode = max(max_decode, active_decode)
                time.sleep(0.02)
                with lock:
                    active_decode -= 1

            if url.endswith("/3.ts"):
                raise RuntimeError("单条线路测试异常")

            return {"status": stream.PASS}

        candidates = [
            {
                "channel": "CCTV-1",
                "normalized_url": f"https://example.com/{i}.ts",
            }
            for i in range(6)
        ]

        with patch("stream.check_stream", side_effect=fake_check):
            results = stream.check_candidates(
                candidates,
                {
                    "stream_probe_concurrency": 2,
                    "stream_decode_concurrency": 1,
                },
            )

        self.assertEqual(
            [item["normalized_url"] for item in results],
            [item["normalized_url"] for item in candidates],
        )
        self.assertEqual(len(results), len(candidates))
        self.assertTrue(all(count == 1 for count in calls.values()))
        self.assertEqual(len(calls), len(candidates))
        self.assertLessEqual(max_probe, 2)
        self.assertGreaterEqual(max_probe, 2)
        self.assertEqual(max_decode, 1)
        self.assertEqual(
            results[3]["stream_quality"]["status"],
            stream.INCOMPLETE,
        )
        self.assertTrue(
            all(
                item["stream_quality"]["status"] == stream.PASS
                for index, item in enumerate(results)
                if index != 3
            )
        )

    def test_check_candidates_rejects_invalid_concurrency(self):
        with self.assertRaises(ValueError):
            stream.check_candidates(
                [],
                {"stream_probe_concurrency": 0},
            )
    
    def test_quality_passed_requires_explicit_pass(self):
        self.assertTrue(
            stream.quality_passed({
                "stream_quality": {"status": stream.PASS}
            })
        )

        self.assertFalse(
            stream.quality_passed({
                "stream_quality": {"status": stream.FAIL}
            })
        )

        self.assertFalse(
            stream.quality_passed({
                "stream_quality": {"status": stream.INCOMPLETE}
            })
        )


class StreamProbeMeasurementTests(unittest.TestCase):

    def probe_with_data(self, data):
        result = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(data),
            stderr="",
        )

        with patch("stream._run", return_value=result):
            return stream._probe("https://example.com/live.ts")

    def base_data(self):
        return {
            "streams": [{
                "codec_type": "video",
                "codec_name": "h264",
                "width": 1920,
                "height": 1080,
                "field_order": "progressive",
            }],
            "frames": [
                {"best_effort_timestamp_time": "0.00"},
                {"best_effort_timestamp_time": "0.04"},
                {"best_effort_timestamp_time": "0.08"},
            ],
        }

    def test_fps_uses_actual_frame_timestamps(self):
        data = self.base_data()

        result = self.probe_with_data(data)

        self.assertEqual(result["fps"], 25.0)
        self.assertEqual(result["scan_type"], "progressive")

    @patch("stream._run")
    def test_probe_limits_frame_read_to_three_seconds(self, run):
        run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(self.base_data()),
            stderr="",
        )

        stream._probe("https://example.com/live.ts")

        command = run.call_args.args[0]

        self.assertEqual(
            command[command.index("-read_intervals") + 1],
            "%+3",
        )
        self.assertEqual(
            command[command.index("-rw_timeout") + 1],
            str(stream.PROBE_RW_TIMEOUT),
        )
        run.assert_called_once_with(
            command,
            stream.PROBE_TIMEOUT,
        )
    
    def test_missing_frame_timestamps_does_not_fallback_to_avg_fps(self):
        data = self.base_data()
        data["frames"] = []

        with self.assertRaises(stream.StreamQualityError):
            self.probe_with_data(data)

    def test_invalid_frame_timestamp_returns_incomplete_data(self):
        data = self.base_data()
        data["frames"][1]["best_effort_timestamp_time"] = "N/A"

        with self.assertRaises(stream.StreamQualityError):
            self.probe_with_data(data)
    def test_out_of_order_frame_timestamps_are_rejected(self):
        data = self.base_data()
        data["frames"][2]["best_effort_timestamp_time"] = "0.02"

        with self.assertRaises(stream.StreamQualityError):
            self.probe_with_data(data)
    
    def test_interlaced_field_order_is_identified(self):
        self.assertEqual(
            stream._classify_scan_type("tt"),
            "interlaced",
        )

    def test_unknown_field_order_is_preserved_as_unknown(self):
        self.assertEqual(
            stream._classify_scan_type(None),
            "unknown",
        )

    def test_invalid_field_order_type_is_unknown(self):
        self.assertEqual(
            stream._classify_scan_type([]),
            "unknown",
        )
        self.assertEqual(
            stream._classify_scan_type({}),
            "unknown",
        )

    def test_probe_rejects_non_object_json(self):
        with self.assertRaises(stream.StreamQualityError):
            self.probe_with_data([])

    def test_probe_rejects_invalid_stream_record(self):
        data = self.base_data()
        data["streams"][0] = None

        with self.assertRaises(stream.StreamQualityError):
            self.probe_with_data(data)

    def test_boolean_frame_timestamp_is_rejected(self):
        self.assertIsNone(
            stream._frame_rate_from_timestamps([
                {"best_effort_timestamp_time": True},
                {"best_effort_timestamp_time": "0.04"},
            ])
        )

class StreamDecodeTests(unittest.TestCase):

    def test_explicit_decoder_error_is_recognized(self):
        self.assertTrue(
            stream._is_explicit_decode_error(
                "non-existing PPS 0 referenced"
            )
        )

        self.assertTrue(
            stream._is_explicit_decode_error(
                "Error while decoding stream"
            )
        )

    def test_network_error_is_not_decoder_error(self):
        self.assertFalse(
            stream._is_explicit_decode_error(
                "Connection timed out"
            )
        )
    
    @patch("stream._run")
    def test_decode_passes_with_30_frames(self, run):
        run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="frame=30\nprogress=end\n",
            stderr="",
        )

        stream._decode("https://example.com/live.ts")

        command = run.call_args.args[0]
        self.assertIn("-progress", command)
        self.assertIn("pipe:1", command)
        self.assertEqual(
            command[command.index("-rw_timeout") + 1],
            str(stream.DECODE_RW_TIMEOUT),
        )
        run.assert_called_once_with(
            command,
            stream.DECODE_TIMEOUT,
        )

    @patch("stream._run")
    def test_decode_rejects_fewer_than_30_frames(self, run):
        run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="frame=10\nprogress=end\n",
            stderr="",
        )

        with self.assertRaises(stream.StreamQualityError):
            stream._decode("https://example.com/live.ts")

    @patch("stream._run")
    def test_decode_rejects_missing_frame_count(self, run):
        run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="progress=end\n",
            stderr="",
        )

        with self.assertRaises(stream.StreamQualityError):
            stream._decode("https://example.com/live.ts")

    @patch("stream._run")
    def test_decode_rejects_ffmpeg_error(self, run):
        run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="frame=10\n",
            stderr="decode error",
        )

        with self.assertRaises(stream.StreamQualityError):
            stream._decode("https://example.com/live.ts")

class StreamFFmpegIntegrationTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            raise unittest.SkipTest("环境没有安装 FFmpeg 或 FFprobe")

        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.video_path = (
            f"{cls.temp_dir.name}/stream-test.mp4"
        )

        result = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel", "error",
                "-f", "lavfi",
                "-i", "color=c=black:s=1920x1080:r=25",
                "-t", "2",
                "-c:v", "mpeg4",
                "-pix_fmt", "yuv420p",
                "-an",
                "-y",
                cls.video_path,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

        if result.returncode != 0:
            cls.temp_dir.cleanup()
            raise RuntimeError(
                f"无法生成真实测试视频：{result.stderr}"
            )

    @classmethod
    def tearDownClass(cls):
        cls.temp_dir.cleanup()

    def test_real_ffprobe_reads_video_timestamps(self):
        result = stream._probe(self.video_path)

        self.assertEqual(result["width"], 1920)
        self.assertEqual(result["height"], 1080)
        self.assertAlmostEqual(result["fps"], 25.0, places=1)

    def test_real_ffmpeg_decodes_30_frames(self):
        stream._decode(self.video_path)


class StreamHTTPHLSIntegrationTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            raise unittest.SkipTest("环境没有安装 FFmpeg 或 FFprobe")

        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.media_path = f"{cls.temp_dir.name}/media.m3u8"
        cls.master_path = f"{cls.temp_dir.name}/master.m3u8"

        result = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel", "error",
                "-f", "lavfi",
                "-i", "testsrc2=size=1920x1080:rate=25",
                "-t", "5",
                "-an",
                "-c:v", "libx264",
                "-preset", "ultrafast",
                "-pix_fmt", "yuv420p",
                "-g", "25",
                "-sc_threshold", "0",
                "-hls_time", "1",
                "-hls_list_size", "0",
                "-hls_segment_filename",
                f"{cls.temp_dir.name}/seg%03d.ts",
                "-f", "hls",
                cls.media_path,
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

        if result.returncode != 0:
            cls.temp_dir.cleanup()
            raise RuntimeError(
                f"无法生成 H.264 HLS 测试视频：{result.stderr}"
            )

        with open(cls.master_path, "w", encoding="utf-8") as file:
            file.write(
                "#EXTM3U\n"
                "#EXT-X-STREAM-INF:BANDWIDTH=3000000,"
                "RESOLUTION=1920x1080\n"
                "media.m3u8\n"
            )

        class RecordingHandler(http.server.SimpleHTTPRequestHandler):

            requests = []

            def __init__(self, *args, **kwargs):
                super().__init__(
                    *args,
                    directory=cls.temp_dir.name,
                    **kwargs,
                )

            def log_message(self, fmt, *args):
                self.requests.append(fmt % args)

        cls.handler = RecordingHandler
        cls.server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0),
            RecordingHandler,
        )
        cls.server.daemon_threads = True

        cls.server_thread = threading.Thread(
            target=cls.server.serve_forever,
            daemon=True,
        )
        cls.server_thread.start()

        cls.url = (
            f"http://127.0.0.1:{cls.server.server_address[1]}"
            "/master.m3u8"
        )

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "server"):
            cls.server.shutdown()
            cls.server.server_close()
            cls.server_thread.join(timeout=5)

        cls.temp_dir.cleanup()

    def test_real_ffprobe_reads_http_hls_timestamps(self):
        self.handler.requests.clear()

        result = stream._probe(self.url)

        self.assertEqual(result["codec"], "h264")
        self.assertEqual(result["width"], 1920)
        self.assertEqual(result["height"], 1080)
        self.assertAlmostEqual(result["fps"], 25.0, places=1)

        requests = self.handler.requests

        self.assertTrue(
            any("GET /master.m3u8" in item for item in requests)
        )
        self.assertTrue(
            any("GET /media.m3u8" in item for item in requests)
        )
        self.assertTrue(
            any(".ts" in item for item in requests)
        )

    def test_real_ffmpeg_decodes_30_http_hls_frames(self):
        self.handler.requests.clear()

        stream._decode(self.url)

        requests = self.handler.requests

        self.assertTrue(
            any("GET /master.m3u8" in item for item in requests)
        )
        self.assertTrue(
            any("GET /media.m3u8" in item for item in requests)
        )
        self.assertTrue(
            any(".ts" in item for item in requests)
        )


if __name__ == "__main__":
    unittest.main()
