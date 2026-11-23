
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""使用 FFprobe 和 FFmpeg 检测并淘汰不合格直播流。"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
from fractions import Fraction
from typing import Any
from urllib.parse import urlsplit


PASS = "PASS"
FAIL = "FAIL"
INCOMPLETE = "INCOMPLETE"

PROBE_TIMEOUT = 12
DECODE_TIMEOUT = 4
MIN_WIDTH = 1920
MIN_HEIGHT = 1080
MIN_FPS = 23.0
MIN_BITRATE = {
    "h264": 5_000_000,
    "hevc": 4_000_000,
}
ALLOWED_CODECS = set(MIN_BITRATE)
BITRATE_SAMPLE_SECONDS = 5
DECODE_FRAMES = 30


class StreamQualityError(Exception):
    """视频质量检测错误。"""


def _valid_url(url: Any) -> bool:
    if not isinstance(url, str):
        return False

    try:
        parsed = urlsplit(url)
    except ValueError:
        return False

    return (
        parsed.scheme in ("http", "https")
        and bool(parsed.hostname)
    )


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
        raise StreamQualityError("视频检测超时") from exc
    except OSError as exc:
        raise StreamQualityError(
            f"无法启动检测程序：{exc}"
        ) from exc


def _parse_fps(value: Any) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None

    try:
        rate = float(Fraction(value))
    except (ValueError, ZeroDivisionError, OverflowError):
        return None

    if not math.isfinite(rate) or rate <= 0:
        return None

    return rate


def _packet_bitrate(packets: Any) -> int | None:
    if not isinstance(packets, list) or not packets:
        return None

    timestamps = []
    total_bytes = 0

    for packet in packets:
        if not isinstance(packet, dict):
            continue

        size = packet.get("size")
        if isinstance(size, str) and size.isdigit():
            size = int(size)

        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            continue

        timestamp = packet.get("pts_time")
        if timestamp is None:
            timestamp = packet.get("dts_time")

        try:
            timestamp = float(timestamp)
        except (TypeError, ValueError):
            continue

        if not math.isfinite(timestamp):
            continue

        total_bytes += size
        timestamps.append(timestamp)

    if total_bytes <= 0 or len(timestamps) < 2:
        return None

    duration = max(timestamps) - min(timestamps)
    if duration <= 0:
        return None

    return round(total_bytes * 8 / duration)


def _probe(url: str) -> dict[str, Any]:
    command = [
        "ffprobe",
        "-v", "error",
        "-rw_timeout", "10000000",
        "-analyzeduration", "5000000",
        "-probesize", "5000000",
        "-read_intervals", f"%+{BITRATE_SAMPLE_SECONDS}",
        "-select_streams", "v:0",
        "-show_streams",
        "-show_packets",
        "-show_entries",
        "stream=codec_type,codec_name,width,height,field_order,"
        "avg_frame_rate,r_frame_rate,bit_rate:"
        "packet=pts_time,dts_time,size",
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

    streams = data.get("streams")
    if not isinstance(streams, list) or not streams:
        raise StreamQualityError("没有检测到有效视频流")

    stream = streams[0]

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


    fps = _parse_fps(stream.get("avg_frame_rate"))

    if fps is None:
        raise StreamQualityError("无法确认实际平均视频帧率")

    bitrate = _packet_bitrate(data.get("packets"))

    if bitrate is None:
        raise StreamQualityError("无法确认实际视频包码率")

    return {
        "codec": codec,
        "width": width,
        "height": height,
        "fps": fps,
        "bitrate": bitrate,
        "field_order": stream.get("field_order"),
    }

def _decode(url: str) -> None:
    command = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel", "error",
        "-xerror",
        "-err_detect", "explode",
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
        raise StreamQualityError(
            f"FFmpeg 解码失败：{result.stderr.strip()[:300]}"
        )


def check_stream(url: str) -> dict[str, Any]:
    """检测单条线路，任何硬性指标不达标均淘汰。"""

    result: dict[str, Any] = {
        "status": INCOMPLETE,
        "width": None,
        "height": None,
        "field_order": None,
        "bitrate": None,
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

        minimum_bitrate = MIN_BITRATE[codec]
        if metadata["bitrate"] < minimum_bitrate:
            result["status"] = FAIL
            result["reason"] = (
                f"{codec} 视频码率低于 "
                f"{minimum_bitrate / 1_000_000:g} Mbps"
            )
            return result

        _decode(url)

        result["status"] = PASS
        result["reason"] = (
            "分辨率、编码、帧率、码率和实际解码检测通过"
        )
        return result

    except StreamQualityError as exc:
        result["status"] = INCOMPLETE
        result["reason"] = str(exc)
        return result


def check_candidates(
    candidates: list[dict[str, Any]],
    detection_settings: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """逐条检测候选线路，保留候选身份和原有数据。"""

    if not isinstance(candidates, list):
        raise ValueError("candidates 必须是列表")

    results = []

    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise ValueError("候选线路数据必须是字典")

        url = candidate.get("normalized_url")
        quality = check_stream(url)

        results.append({
            **candidate,
            "stream_quality": quality,
        })

    return results


def quality_passed(candidate: dict[str, Any]) -> bool:
    """只有明确通过全部视频质量检测的线路才允许继续。"""

    if not isinstance(candidate, dict):
        return False

    quality = candidate.get("stream_quality")

    return (
        isinstance(quality, dict)
        and quality.get("status") == PASS
    )
