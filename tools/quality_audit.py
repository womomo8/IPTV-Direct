#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读审计 IPTV 仓库现有候选线路的真实分辨率和解码情况。"""

from __future__ import annotations

import csv
import json
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import collector
import main
import normalizer


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "quality_audit"
WORKERS = 8
PROBE_TIMEOUT = 15
DECODE_TIMEOUT = 15
DECODE_SECONDS = 5


def safe_url(value: str) -> str:
    """报告中隐藏 URL 查询参数，避免泄露访问令牌。"""
    try:
        parsed = urlsplit(value)
        return urlunsplit((
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            "",
            "",
        ))
    except ValueError:
        return ""


def run_command(command: list[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def inspect_candidate(candidate: dict) -> dict:
    """对一条候选线路读取真实媒体信息并尝试解码。"""
    url = candidate["normalized_url"]

    result = {
        "channel": candidate["channel"],
        "source_name": candidate.get("name", ""),
        "source_url": safe_url(candidate.get("source_url", "")),
        "stream_url": safe_url(url),
        "width": None,
        "height": None,
        "field_order": "",
        "codec": "",
        "video_bitrate": None,
        "format_bitrate": None,
        "probe_status": "INCOMPLETE",
        "decode_status": "NOT_RUN",
        "quality_status": "INCOMPLETE",
        "reason": "",
    }

    probe_command = [
        "ffprobe",
        "-v", "error",
        "-rw_timeout", "12000000",
        "-analyzeduration", "5000000",
        "-probesize", "5000000",
        "-read_intervals", "%+6",
        "-show_entries",
        "stream=codec_type,codec_name,width,height,field_order,bit_rate:"
        "format=bit_rate",
        "-of", "json",
        url,
    ]

    try:
        probe = run_command(probe_command, PROBE_TIMEOUT)
    except subprocess.TimeoutExpired:
        result["reason"] = "FFprobe 检测超时"
        result["probe_status"] = "TIMEOUT"
        return result
    except OSError as exc:
        result["reason"] = f"无法启动 FFprobe：{exc}"
        return result

    if probe.returncode != 0:
        result["reason"] = (
            probe.stderr.strip()[-1000:] or "FFprobe 未能读取媒体信息"
        )
        result["probe_status"] = "FAILED"
        return result

    try:
        metadata = json.loads(probe.stdout)
    except json.JSONDecodeError:
        result["reason"] = "FFprobe 返回内容不是有效 JSON"
        result["probe_status"] = "FAILED"
        return result

    streams = metadata.get("streams", [])
    video_streams = [
        stream for stream in streams
        if stream.get("codec_type") == "video"
    ]

    if not video_streams:
        result["reason"] = "没有检测到视频流"
        result["probe_status"] = "NO_VIDEO"
        return result

    video = video_streams[0]
    media_format = metadata.get("format", {})

    result["width"] = video.get("width")
    result["height"] = video.get("height")
    result["field_order"] = video.get("field_order", "")
    result["codec"] = video.get("codec_name", "")
    result["video_bitrate"] = video.get("bit_rate")
    result["format_bitrate"] = media_format.get("bit_rate")
    result["probe_status"] = "PASS"

    decode_command = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-v", "error",
        "-rw_timeout", "12000000",
        "-i", url,
        "-t", str(DECODE_SECONDS),
        "-map", "0:v:0",
        "-f", "null",
        "-",
    ]

    try:
        decoded = run_command(decode_command, DECODE_TIMEOUT)
    except subprocess.TimeoutExpired:
        result["decode_status"] = "TIMEOUT"
        result["reason"] = "视频解码超时"
        result["quality_status"] = "DECODE_FAILED"
        return result
    except OSError as exc:
        result["decode_status"] = "FAILED"
        result["reason"] = f"无法启动 FFmpeg：{exc}"
        result["quality_status"] = "DECODE_FAILED"
        return result

    if decoded.returncode != 0:
        result["decode_status"] = "FAILED"
        result["reason"] = (
            decoded.stderr.strip()[-1000:] or "视频解码失败"
        )
        result["quality_status"] = "DECODE_FAILED"
        return result

    result["decode_status"] = "PASS"

    width = result["width"]
    height = result["height"]
    field_order = str(result["field_order"]).lower()

    if not isinstance(width, int) or not isinstance(height, int):
        result["quality_status"] = "UNKNOWN_RESOLUTION"
        result["reason"] = "无法确认真实视频分辨率"
    elif width < 1920 or height < 1080:
        result["quality_status"] = "BELOW_1080P"
        result["reason"] = "真实分辨率低于 1920×1080"
    elif field_order != "progressive":
        if field_order in ("tt", "bb", "tb", "bt"):
            result["quality_status"] = "INTERLACED"
            result["reason"] = f"检测到隔行扫描：{field_order}"
        else:
            result["quality_status"] = "UNKNOWN_FIELD_ORDER"
            result["reason"] = "无法确认是否为逐行扫描"
    else:
        result["quality_status"] = "PASS_1080P"
        result["reason"] = "真实分辨率达标、逐行扫描且解码通过"

    return result


def collect_candidates() -> tuple[list[dict], list[dict]]:
    """沿用仓库现有来源下载、频道匹配、别名和 URL 去重规则。"""
    config = main.load_configuration(ROOT)
    settings = config["settings"]
    channels = config["channels"]
    seen_urls: set[str] = set()

    candidates = []
    source_errors = []
    valid_sources = 0

    source_items = collector.iter_source_contents(
        config["manual_sources"],
        config["automatic_sources"],
        settings,
    )

    for item in source_items:
        source = item.get("source", {})
        downloaded = item.get("download")

        if downloaded is None:
            source_errors.append({
                "source": source.get("name", source.get("url", "")),
                "errors": item.get("errors", []),
            })
            continue

        source_info = {
            **source,
            "final_url": downloaded.get("final_url", source.get("url", "")),
        }

        remaining = (
            settings["collection"]["max_candidates_total"]
            - len(candidates)
        )
        if remaining <= 0:
            break

        try:
            parsed = normalizer.normalize_source_content(
                downloaded["content"],
                source_info,
                channels,
                settings["collection"]["max_candidates_per_source"],
                seen_urls,
                content_type=downloaded.get("content_type", ""),
                max_hls_manifest_bytes=settings["detection"][
                    "max_hls_manifest_bytes"
                ],
                aliases=settings.get("aliases", {}),
                max_candidates_remaining=remaining,
                channels_path=str(ROOT / "channels.txt"),
            )
        except Exception as exc:
            source_errors.append({
                "source": source.get("name", source.get("url", "")),
                "errors": [f"来源解析异常：{exc}"],
            })
            continue

        if parsed["candidates"]:
            valid_sources += 1

        for candidate in parsed["candidates"]:
            candidates.append({
                **candidate,
                "name": source.get("name", source.get("id", "")),
            })

        if valid_sources >= settings["collection"]["max_valid_sources"]:
            break

    return candidates, source_errors


def write_reports(results: list[dict], source_errors: list[dict]) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    channels = main.load_configuration(ROOT)["channels"]
    summary = []

    for channel in channels:
        items = [item for item in results if item["channel"] == channel]
        summary.append({
            "channel": channel,
            "candidate_count": len(items),
            "confirmed_1080p": sum(
                item["quality_status"] == "PASS_1080P" for item in items
            ),
            "below_1080p": sum(
                item["quality_status"] == "BELOW_1080P" for item in items
            ),
            "interlaced": sum(
                item["quality_status"] == "INTERLACED" for item in items
            ),
            "unknown_quality": sum(
                item["quality_status"] in (
                    "INCOMPLETE",
                    "UNKNOWN_RESOLUTION",
                    "UNKNOWN_FIELD_ORDER",
                    "DECODE_FAILED",
                )
                for item in items
            ),
        })

    report = {
        "notice": (
            "只读审计结果，不代表正式线路准入；"
            "1080P 仅统计真实逐行分辨率且解码通过的候选。"
        ),
        "candidate_total": len(results),
        "confirmed_1080p_total": sum(
            item["quality_status"] == "PASS_1080P" for item in results
        ),
        "source_errors": source_errors,
        "channels": summary,
        "candidates": results,
    }

    json_path = OUTPUT_DIR / "quality_audit.json"
    with json_path.open("w", encoding="utf-8") as file:
        json.dump(report, file, ensure_ascii=False, indent=2)

    csv_path = OUTPUT_DIR / "quality_audit.csv"
    fields = [
        "channel", "source_name", "source_url", "stream_url",
        "width", "height", "field_order", "codec",
        "video_bitrate", "format_bitrate",
        "probe_status", "decode_status", "quality_status", "reason",
    ]

    with csv_path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(results)

    summary_path = OUTPUT_DIR / "channel_summary.csv"
    with summary_path.open("w", encoding="utf-8-sig", newline="") as file:
        fields = [
            "channel", "candidate_count", "confirmed_1080p",
            "below_1080p", "interlaced", "unknown_quality",
        ]
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(summary)

    print(f"候选总数：{len(results)}")
    print(f"确认 1080P 且解码通过：{report['confirmed_1080p_total']}")
    print(f"报告目录：{OUTPUT_DIR}")


def main_run() -> int:
    if not shutil.which("ffprobe") or not shutil.which("ffmpeg"):
        print("错误：系统没有安装 FFmpeg/FFprobe。", file=sys.stderr)
        return 1

    print("正在读取仓库现有来源和目标频道……")
    candidates, source_errors = collect_candidates()
    print(f"收集到 {len(candidates)} 条去重后的目标频道候选。")

    results = []
    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = {
            executor.submit(inspect_candidate, candidate): candidate
            for candidate in candidates
        }

        for index, future in enumerate(as_completed(futures), start=1):
            candidate = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                result = {
                    "channel": candidate.get("channel", ""),
                    "source_name": candidate.get("name", ""),
                    "source_url": safe_url(candidate.get("source_url", "")),
                    "stream_url": safe_url(candidate.get("normalized_url", "")),
                    "width": None,
                    "height": None,
                    "field_order": "",
                    "codec": "",
                    "video_bitrate": None,
                    "format_bitrate": None,
                    "probe_status": "FAILED",
                    "decode_status": "NOT_RUN",
                    "quality_status": "INCOMPLETE",
                    "reason": f"检测程序异常：{exc}",
                }

            results.append(result)

            if index % 20 == 0 or index == len(candidates):
                print(f"已检测 {index}/{len(candidates)} 条候选。")

    write_reports(results, source_errors)
    return 0


if __name__ == "__main__":
    raise SystemExit(main_run())
