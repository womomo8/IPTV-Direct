
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""使用 FFprobe 和 FFmpeg 检测并淘汰不合格直播流。"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
from typing import Any
from urllib.parse import urlsplit
from concurrent.futures import ThreadPoolExecutor
from threading import BoundedSemaphore

PASS = "PASS"
FAIL = "FAIL"
INCOMPLETE = "INCOMPLETE"

PROBE_TIMEOUT = 12
DECODE_TIMEOUT = 4
MIN_WIDTH = 1920
MIN_HEIGHT = 1080
MIN_FPS = 23.0
ALLOWED_CODECS = {"h264", "hevc"}
DECODE_FRAMES = 30

class StreamQualityError(Exception):
    """视频质量检测错误。"""

class StreamDecodeError(StreamQualityError):
    """已经取得明确证据的视频解码失败。"""


_DECODE_ERROR_MARKERS = (
    "error while decoding",
    "decode error",
    "decoding error",
    "non-existing pps",
    "decode_slice_header",
    "invalid nal unit",
)


def _is_explicit_decode_error(stderr: Any) -> bool:
    """只有 FFmpeg 输出明确的解码错误特征时才认定解码失败。"""
    message = str(stderr or "").lower()
    return any(
        marker in message
        for marker in _DECODE_ERROR_MARKERS
    )

def _valid_url(url: Any) -> bool:
    if not isinstance(url, str):
        return False

    try:
        parsed = urlsplit(url)
        _ = parsed.port
    except ValueError:
        return False

    return (
        parsed.scheme in ("http", "https")
        and bool(parsed.hostname)
    )

def _classify_scan_type(field_order: Any) -> str:
    if not isinstance(field_order, str):
        return "unknown"

    value = field_order.strip().lower()

    if value == "progressive":
        return "progressive"

    if value in {"tt", "bb", "tb", "bt"}:
        return "interlaced"

    return "unknown"

def _run(command: list[str], timeout: int) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise StreamQualityError(
            f"{command[0]}执行超时（限制 {timeout} 秒）"
        ) from exc
    except OSError as exc:
        raise StreamQualityError(
            f"无法启动检测程序：{exc}"
        ) from exc

def _frame_rate_from_timestamps(frames: Any) -> float | None:
    if not isinstance(frames, list) or len(frames) < 2:
        return None

    timestamps = []

    for frame in frames:
        if not isinstance(frame, dict):
            return None

        value = frame.get("best_effort_timestamp_time")

        if isinstance(value, bool):
            return None

        try:
            timestamp = float(value)
        except (TypeError, ValueError):
            return None

        if not math.isfinite(timestamp):
            return None

        timestamps.append(timestamp)

    if any(
        current <= previous
        for previous, current in zip(
            timestamps,
            timestamps[1:],
        )
    ):
        return None

    duration = timestamps[-1] - timestamps[0]

    if duration <= 0:
        return None

    fps = (len(timestamps) - 1) / duration

    if not math.isfinite(fps) or fps <= 0:
        return None

    return fps

def _probe(url: str) -> dict[str, Any]:
    """读取视频流元数据和实际帧时间戳。"""

    command = [
        "ffprobe",
        "-v", "error",
        "-rw_timeout", "10000000",
        "-analyzeduration", "5000000",
        "-probesize", "5000000",
        "-select_streams", "v:0",
        "-show_streams",
        "-show_frames",
        "-read_intervals", "%+3",
        "-show_entries",
        "stream=codec_type,codec_name,width,height,field_order:"
        "frame=best_effort_timestamp_time",
        "-of", "json",
        url,
    ]

    result = _run(command, PROBE_TIMEOUT)

    if result.returncode != 0:
        raise StreamQualityError(
            f"FFprobe 检测失败：{result.stderr.strip()[:300]}"
        )

    try:
        data = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise StreamQualityError(
            "FFprobe 返回的数据无法解析"
        ) from exc

    if not isinstance(data, dict):
        raise StreamQualityError("FFprobe 返回的数据结构无效")

    streams = data.get("streams")
    if not isinstance(streams, list) or not streams:
        raise StreamQualityError("没有检测到有效视频流")

    stream = streams[0]

    if not isinstance(stream, dict):
        raise StreamQualityError("FFprobe 返回的视频流结构无效")

    if stream.get("codec_type") != "video":
        raise StreamQualityError("检测到的媒体流不是视频流")

    codec = stream.get("codec_name")
    if not isinstance(codec, str) or not codec:
        raise StreamQualityError("无法确认视频编码")

    width = stream.get("width")
    height = stream.get("height")

    if (
        isinstance(width, bool)
        or not isinstance(width, int)
        or isinstance(height, bool)
        or not isinstance(height, int)
        or width <= 0
        or height <= 0
    ):
        raise StreamQualityError("无法确认真实视频分辨率")

    fps = _frame_rate_from_timestamps(data.get("frames"))

    if fps is None:
        raise StreamQualityError(
            "无法根据实际视频帧时间戳确认平均帧率"
        )

    field_order = stream.get("field_order")

    return {
        "codec": codec,
        "width": width,
        "height": height,
        "fps": fps,
        "field_order": field_order,
        "scan_type": _classify_scan_type(field_order),
    }

def _decode(url: str) -> None:
    command = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel", "error",
        "-xerror",
        "-err_detect", "explode",
        "-progress", "pipe:1",
        "-rw_timeout", "4000000",
        "-i", url,
        "-map", "0:v:0",
        "-an",
        "-frames:v", str(DECODE_FRAMES),
        "-f", "null",
        "-",
    ]

    result = _run(command, DECODE_TIMEOUT)

    if result.returncode != 0:
        error_message = result.stderr.strip()[:300]

        if _is_explicit_decode_error(result.stderr):
            raise StreamDecodeError(
                f"FFmpeg 确认视频解码失败：{error_message}"
            )

        raise StreamQualityError(
            f"FFmpeg 执行失败，无法确认是否为解码错误："
            f"{error_message}"
        )

    decoded_frames = None

    for line in result.stdout.splitlines():
        if line.startswith("frame="):
            try:
                decoded_frames = int(
                    line.partition("=")[2].strip()
                )
            except ValueError:
                decoded_frames = None

    if decoded_frames is None:
        raise StreamQualityError("无法确认实际解码帧数")

    if decoded_frames < DECODE_FRAMES:
        raise StreamQualityError(
            f"实际解码帧数不足："
            f"{decoded_frames}/{DECODE_FRAMES}"
        )


def check_stream(
    url: str,
    probe_semaphore: BoundedSemaphore | None = None,
    decode_semaphore: BoundedSemaphore | None = None,
) -> dict[str, Any]:

    result: dict[str, Any] = {
        "status": INCOMPLETE,
        "width": None,
        "height": None,
        "field_order": None,
        "codec": None,
        "fps": None,
        "reason": "",
    }

    if not _valid_url(url):
        result["status"] = FAIL
        result["reason"] = "不是有效的 HTTP/HTTPS 直播地址"
        return result

    if not shutil.which("ffprobe") or not shutil.which("ffmpeg"):
        result["reason"] = "运行环境缺少 FFprobe 或 FFmpeg"
        return result

    try:
        if probe_semaphore is None:
            metadata = _probe(url)
        else:
            with probe_semaphore:
                metadata = _probe(url)

        result.update(metadata)

        if (
            metadata["width"] < MIN_WIDTH
            or metadata["height"] < MIN_HEIGHT
        ):
            result["status"] = FAIL
            result["reason"] = "实际分辨率低于 1920×1080"
            return result

        codec = metadata["codec"].lower()
        if codec not in ALLOWED_CODECS:
            result["status"] = FAIL
            result["reason"] = f"不支持的视频编码：{codec}"
            return result

        if metadata["fps"] < MIN_FPS:
            result["status"] = FAIL
            result["reason"] = (
                f"实际帧率低于 {MIN_FPS:g} fps"
            )
            return result

        if decode_semaphore is None:
            _decode(url)
        else:
            with decode_semaphore:
                _decode(url)

        result["status"] = PASS
        result["reason"] = (
            "分辨率、编码、实际帧率和30帧解码检测通过"
        )
        return result

    except StreamDecodeError as exc:
        result["status"] = FAIL
        result["reason"] = str(exc)
        return result

    except Exception as exc:
        result["status"] = INCOMPLETE
        result["reason"] = (
            f"视频检测发生未预期异常"
            f"（{type(exc).__name__}）：{exc}"
        )
        return result

def check_candidates(
    candidates: list[dict[str, Any]],
    detection_settings: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """并发检测候选线路，保留候选身份和原有顺序。"""

    if not isinstance(candidates, list):
        raise ValueError("candidates 必须是列表")

    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise ValueError("候选线路数据必须是字典")

    if detection_settings is None:
        detection_settings = {}

    if not isinstance(detection_settings, dict):
        raise ValueError("detection_settings 必须是字典")

    probe_concurrency = detection_settings.get(
        "stream_probe_concurrency", 4
    )
    decode_concurrency = detection_settings.get(
        "stream_decode_concurrency", 2
    )

    if (
        isinstance(probe_concurrency, bool)
        or not isinstance(probe_concurrency, int)
        or probe_concurrency < 1
    ):
        raise ValueError("FFprobe 并发数必须是正整数")

    if (
        isinstance(decode_concurrency, bool)
        or not isinstance(decode_concurrency, int)
        or decode_concurrency < 1
    ):
        raise ValueError("FFmpeg 并发数必须是正整数")

    probe_semaphore = BoundedSemaphore(probe_concurrency)
    decode_semaphore = BoundedSemaphore(decode_concurrency)

    def check_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
        url = candidate.get("normalized_url")

        try:
            quality = check_stream(
                url,
                probe_semaphore,
                decode_semaphore,
            )
        except Exception as exc:
            quality = {
                "status": INCOMPLETE,
                "width": None,
                "height": None,
                "field_order": None,
                "codec": None,
                "fps": None,
                "reason": (
                    f"视频检测发生未预期异常"
                    f"（{type(exc).__name__}）：{exc}"
                ),
            }

        return {
            **candidate,
            "stream_quality": quality,
        }

    with ThreadPoolExecutor(
        max_workers=probe_concurrency + decode_concurrency
    ) as executor:
        return list(
            executor.map(check_candidate, candidates)
        )

def quality_passed(candidate: dict[str, Any]) -> bool:
    """只有明确通过全部视频质量检测的线路才允许继续。"""

    if not isinstance(candidate, dict):
        return False

    quality = candidate.get("stream_quality")

    return (
        isinstance(quality, dict)
        and quality.get("status") == PASS
    )
