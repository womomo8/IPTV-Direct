
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


PASS = "PASS"
FAIL = "FAIL"
INCOMPLETE = "INCOMPLETE"

PROBE_TIMEOUT = 12
DECODE_TIMEOUT = 4
MIN_WIDTH = 1920
MIN_HEIGHT = 1080
MIN_FPS = 23.0
MIN_WIDTH = 1920
MIN_HEIGHT = 1080
MIN_FPS = 23.0
ALLOWED_CODECS = {"h264", "hevc"}
DECODE_FRAMES = 30

class StreamQualityError(Exception):
    """视频质量检测错误。"""


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

def _classify_scan_type(field_order: Any) -> str:
    if not isinstance(field_order, str):
        return "unknown"

    if field_order == "progressive":
        return "progressive"

    if field_order in {"tt", "bb", "tb", "bt"}:
        return "interlaced"

    return "unknown"

    command = [
        "ffprobe",
        "-v", "error",
        "-rw_timeout", "10000000",
        "-analyzeduration", "5000000",
        "-probesize", "5000000",
        "-select_streams", "v:0",
        "-show_streams",
        "-show_frames",
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
        raise StreamQualityError(
            f"FFmpeg 解码失败：{result.stderr.strip()[:300]}"
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


def check_stream(url: str) -> dict[str, Any]:
    """检测单条线路，任何硬性指标不达标均淘汰。"""

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

        if not BITRATE_SAMPLE_VALIDATED:
            result["status"] = INCOMPLETE
            result["reason"] = (
                "5 秒码率观察窗口尚未通过真实线路样本验证"
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

        result["reason"] = (
            "分辨率、编码、实际帧率和30帧解码检测通过"
        )
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
