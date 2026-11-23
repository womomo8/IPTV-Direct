
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""使用 FFprobe 和 FFmpeg 检测直播流的真实视频质量。"""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any
from urllib.parse import urlsplit


PASS = "PASS"
FAIL = "FAIL"
INCOMPLETE = "INCOMPLETE"

PROBE_TIMEOUT = 4
DECODE_TIMEOUT = 4
MIN_WIDTH = 1920
MIN_HEIGHT = 1080
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
        raise StreamQualityError(f"无法启动检测程序：{exc}") from exc


def _probe(url: str) -> dict[str, Any]:
    command = [
        "ffprobe",
        "-v", "error",
        "-rw_timeout", "4000000",
        "-analyzeduration", "3000000",
        "-probesize", "3000000",
        "-select_streams", "v:0",
        "-show_entries",
        "stream=codec_name,width,height,field_order,bit_rate:"
        "format=bit_rate",
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
        raise StreamQualityError("FFprobe 返回的数据无法解析") from exc

    streams = data.get("streams")
    if not isinstance(streams, list) or not streams:
        raise StreamQualityError("无法确认视频流信息")

    stream = streams[0]

    width = stream.get("width")
    height = stream.get("height")
    field_order = stream.get("field_order")

    if (
        isinstance(width, bool)
        or not isinstance(width, int)
        or isinstance(height, bool)
        or not isinstance(height, int)
    ):
        raise StreamQualityError("无法确认真实视频分辨率")

    if not isinstance(field_order, str) or not field_order:
        raise StreamQualityError("无法确认视频扫描方式")

    bitrate = stream.get("bit_rate") or data.get("format", {}).get("bit_rate")

    try:
        bitrate = int(bitrate) if bitrate is not None else None
    except (TypeError, ValueError):
        bitrate = None

    return {
        "codec": stream.get("codec_name"),
        "width": width,
        "height": height,
        "field_order": field_order,
        "bitrate": bitrate,
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
    """检测单条线路。每次调用只执行一次检测。"""

    result: dict[str, Any] = {
        "status": INCOMPLETE,
        "width": None,
        "height": None,
        "field_order": None,
        "bitrate": None,
        "codec": None,
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

        if metadata["field_order"] != "progressive":
            result["status"] = FAIL
            result["reason"] = (
                "不是已确认的逐行扫描视频，不能认定为 1080P"
            )
            return result

        _decode(url)

        result["status"] = PASS
        result["reason"] = "分辨率、逐行扫描和实际解码检测通过"
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
    """只有明确通过视频质量检测的线路才允许继续。"""

    if not isinstance(candidate, dict):
        return False

    quality = candidate.get("stream_quality")

    return (
        isinstance(quality, dict)
        and quality.get("status") == PASS
    )
