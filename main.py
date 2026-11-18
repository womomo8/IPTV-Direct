#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IPTV-Direct 总入口：多来源汇集、统一检测、优选和订阅发布。"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unittest
from collections import OrderedDict
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import checker
import globalping
import publisher

from collector import SourceConfigError, iter_source_contents, prepare_sources
from normalizer import ChannelConfigError, load_channels, normalize_source_content

ROOT = Path(__file__).resolve().parent
CONFIG_FILES = ("channels.txt", "manual.txt", "sources.json", "settings.json")

PASS = "PASS"
FAIL = "FAIL"
INCOMPLETE = "INCOMPLETE"


# ----------------------------------------------------------------------
# 配置读取
# ----------------------------------------------------------------------

def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except FileNotFoundError as exc:
        raise ValueError(f"缺少配置文件：{path.name}") from exc
    except UnicodeDecodeError as exc:
        raise ValueError(f"配置文件不是有效 UTF-8 文本：{path.name}") from exc
    except OSError as exc:
        raise ValueError(f"读取配置文件失败：{path.name}：{exc}") from exc


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(_read_text(path))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"JSON 格式错误：{path.name}，第 {exc.lineno} 行：{exc.msg}"
        ) from exc

    if not isinstance(value, dict):
        raise ValueError(f"配置文件顶层必须是 JSON 对象：{path.name}")

    return value


def _check_settings(settings: dict[str, Any]) -> None:
    required = {
        "collection": (
            "max_valid_sources",
            "max_candidates_per_source",
            "max_candidates_total",
            "max_index_links_per_page",
        ),
        "detection": (
            "local_concurrency",
            "per_host_concurrency",
            "queue_limit",
            "max_queue_seconds",
            "candidate_active_budget_seconds",
            "source_download_timeout_seconds",
            "max_source_download_bytes",
            "max_hls_manifest_bytes",
            "max_ts_read_bytes",
            "max_redirects",
        ),
        "globalping": ("max_probes",),
        "selection": ("max_routes_per_channel",),
        "schedule": ("timezone", "daily_time"),
    }

    for section, keys in required.items():
        data = settings.get(section)

        if not isinstance(data, dict):
            raise ValueError(f"settings.json 缺少有效配置区段：{section}")

        for key in keys:
            value = data.get(key)

            if key in ("timezone", "daily_time"):
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(
                        f"settings.json 参数 {section}.{key} 必须是非空文本"
                    )
            elif (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value <= 0
            ):
                raise ValueError(
                    f"settings.json 参数 {section}.{key} 必须是正整数"
                )

    if settings["schedule"]["timezone"] != "Asia/Shanghai":
        raise ValueError(
            "settings.json schedule.timezone 必须为 Asia/Shanghai"
        )

    clock = settings["schedule"]["daily_time"]

    try:
        hour, minute = (int(part) for part in clock.split(":"))
    except (ValueError, AttributeError) as exc:
        raise ValueError(
            "settings.json schedule.daily_time 必须使用 HH:MM 格式"
        ) from exc

    if len(clock) != 5 or not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(
            "settings.json schedule.daily_time 不是有效 HH:MM 时间"
        )


def load_configuration(
    root: Path = ROOT,
    test_mode: bool = False,
) -> dict[str, Any]:
    for name in CONFIG_FILES:
        if not (root / name).is_file():
            raise ValueError(f"缺少配置文件：{name}")

    try:
        channels = load_channels(str(root / "channels.txt"))
    except ChannelConfigError as exc:
        if test_mode and str(exc) == "channels.txt 没有配置目标频道":
            channels = []
        else:
            raise ValueError(str(exc)) from exc

    try:
        prepared = prepare_sources(
            _read_text(root / "manual.txt"),
            _read_json(root / "sources.json"),
        )
    except SourceConfigError as exc:
        raise ValueError(str(exc)) from exc

    settings = _read_json(root / "settings.json")
    _check_settings(settings)

    aliases = settings.get("aliases", {})

    if not isinstance(aliases, dict):
        raise ValueError("settings.json aliases 必须是字典")

    seen_aliases = set()

    for alias, canonical in aliases.items():
        if (
            not isinstance(alias, str)
            or not alias.strip()
            or not isinstance(canonical, str)
            or not canonical.strip()
        ):
            raise ValueError(
                f"settings.json 存在无效频道别名：{alias} -> {canonical}"
            )

        alias_key = alias.casefold()

        if alias_key in seen_aliases:
            raise ValueError(f"settings.json 存在重复频道别名：{alias}")

        seen_aliases.add(alias_key)

    return {
        "channels": channels,
        **prepared,
        "settings": settings,
    }


# ----------------------------------------------------------------------
# 候选身份与质量优选
# ----------------------------------------------------------------------

def _identity(item: dict[str, Any]) -> tuple[Any, Any]:
    return item.get("channel"), item.get("normalized_url")


def _quality_number(
    quality: dict[str, Any],
    key: str,
) -> float | None:
    value = quality.get(key)

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None

    return float(value)


def _known_first(
    value: float | None,
    reverse: bool = False,
) -> tuple[int, float]:
    """已测得的数据优先；缺失数据不伪装成 0 或最好值。"""
    if value is None:
        return (1, 0.0)

    return (0, -value if reverse else value)


def _candidate_quality_key(
    candidate: dict[str, Any],
) -> tuple[Any, ...]:
    """
    使用 checker 已记录的真实质量数据分层比较。
    不计算综合分数，也不改变任何 PASS/FAIL/INCOMPLETE 结果。
    """
    quality = candidate.get("quality")

    if not isinstance(quality, dict):
        quality = {}

    media_type = candidate.get("media_type")

    request_count = _quality_number(quality, "request_count")
    success_count = _quality_number(quality, "request_success_count")
    timeout_count = _quality_number(quality, "timeout_count")

    total_elapsed = _quality_number(quality, "total_elapsed_seconds")
    total_read = _quality_number(quality, "total_read_seconds")
    total_bytes = _quality_number(quality, "total_bytes_read")

    success_ratio = None
    if (
        request_count is not None
        and request_count > 0
        and success_count is not None
    ):
        success_ratio = success_count / request_count

    average_elapsed = None
    if (
        request_count is not None
        and request_count > 0
        and total_elapsed is not None
    ):
        average_elapsed = total_elapsed / request_count

    average_read = None
    if (
        request_count is not None
        and request_count > 0
        and total_read is not None
    ):
        average_read = total_read / request_count

    throughput = None
    if (
        total_read is not None
        and total_read > 0
        and total_bytes is not None
    ):
        throughput = total_bytes / total_read

    parts: list[tuple[int, float]] = []

    # HLS 比较实际媒体分片成功情况；直接 TS 不使用此指标。
    if media_type == "HLS":
        segment_total = _quality_number(quality, "segment_total")
        segment_success = _quality_number(
            quality,
            "segment_success_count",
        )

        segment_ratio = None

        if (
            segment_total is not None
            and segment_total > 0
            and segment_success is not None
        ):
            segment_ratio = segment_success / segment_total

        parts.append(_known_first(segment_ratio, reverse=True))
        parts.append(_known_first(segment_success, reverse=True))

    parts.append(_known_first(timeout_count))
    parts.append(_known_first(success_ratio, reverse=True))
    parts.append(_known_first(average_elapsed))
    parts.append(_known_first(average_read))
    parts.append(_known_first(throughput, reverse=True))

    return tuple(parts)


def rank_candidates_for_globalping(
    candidates: list[dict[str, Any]],
    channels: list[str] | None = None,
) -> list[dict[str, Any]]:
    """
    同频道、同媒体类型内优选。

    各频道/媒体类型组轮流交接，避免某个频道的候选
    一次性占满 Globalping 额度。
    """
    if not candidates:
        return []

    channel_order = {
        channel: index
        for index, channel in enumerate(channels or [])
    }

    groups: OrderedDict[
        tuple[str, str],
        list[tuple[int, dict[str, Any]]],
    ] = OrderedDict()

    for index, candidate in enumerate(candidates):
        channel = str(candidate.get("channel", ""))
        media_type = str(candidate.get("media_type", "UNKNOWN"))
        key = (channel, media_type)
        groups.setdefault(key, []).append((index, candidate))

    # 先固定每组首次出现顺序，再排序；不在排序过程中反查列表。
    original_group_order = {
        key: index
        for index, key in enumerate(groups.keys())
    }

    group_keys = sorted(
        groups.keys(),
        key=lambda key: (
            channel_order.get(key[0], len(channel_order)),
            original_group_order[key],
        ),
    )

    queues: list[list[dict[str, Any]]] = []

    for key in group_keys:
        indexed = groups[key]

        indexed.sort(
            key=lambda pair: (
                _candidate_quality_key(pair[1]),
                pair[0],
            )
        )

        queues.append([
            candidate
            for _, candidate in indexed
        ])

    ranked: list[dict[str, Any]] = []
    depth = 0

    while True:
        added = False

        for queue in queues:
            if depth < len(queue):
                ranked.append(queue[depth])
                added = True

        if not added:
            break

        depth += 1

    return ranked


# ----------------------------------------------------------------------
# 本地 checker 与 Globalping 检测交接
# ----------------------------------------------------------------------

def run_detection_pipeline(
    candidates: list[dict[str, Any]],
    settings: dict[str, Any],
    local_check=checker.check_candidates,
    remote_check=globalping.check_candidates,
    channels: list[str] | None = None,
) -> list[dict[str, Any]]:
    """
    全部候选统一执行本地检测。

    本地适用检测全部 PASS 后，才按质量优先顺序交给 Globalping。
    返回结果恢复候选池原始顺序，不改变 publisher 的来源政策。
    """
    if not candidates:
        return []

    originals = [_identity(item) for item in candidates]

    try:
        local_results = local_check(
            candidates,
            settings["detection"],
        )

        if (
            not isinstance(local_results, list)
            or len(local_results) != len(candidates)
        ):
            raise ValueError(
                "checker 返回数量与输入候选数量不一致"
            )

        if any(
            not isinstance(item, dict)
            or _identity(item) != originals[index]
            for index, item in enumerate(local_results)
        ):
            raise ValueError(
                "checker 返回顺序或候选身份不匹配"
            )

    except Exception as exc:
        reason = (
            f"本地检测调用异常或结果校验失败：{exc}"
        )

        local_results = [
            {
                **item,
                "media_type": "UNKNOWN",
                "checks": {
                    key: {
                        "status": INCOMPLETE,
                        "reason": reason,
                    }
                    for key in ("ipv4", "http", "hls", "ts")
                },
            }
            for item in candidates
        ]

    eligible_indices = [
        index
        for index, item in enumerate(local_results)
        if checker.local_checks_passed(item)
    ]

    eligible = [
        local_results[index]
        for index in eligible_indices
    ]

    eligible_set = set(eligible_indices)
    output: list[dict[str, Any] | None] = [
        None
    ] * len(local_results)

    for index, item in enumerate(local_results):
        if index not in eligible_set:
            output[index] = globalping.incomplete_result(
                item,
                (
                    "本地检测未全部通过，未提交 Globalping；"
                    "不代表远程检测失败。"
                ),
            )

    if eligible:
        ranked = rank_candidates_for_globalping(
            eligible,
            channels,
        )

        try:
            remote_results = remote_check(
                ranked,
                settings["globalping"],
            )

            if (
                not isinstance(remote_results, list)
                or len(remote_results) != len(ranked)
            ):
                raise ValueError(
                    "Globalping 返回数量与输入候选数量不一致"
                )

            for position, result in enumerate(remote_results):
                if (
                    not isinstance(result, dict)
                    or _identity(result) != _identity(ranked[position])
                ):
                    raise ValueError(
                        "Globalping 返回顺序或候选身份不匹配"
                    )

                remote = result.get("globalping")

                if (
                    not isinstance(remote, dict)
                    or remote.get("status")
                    not in (PASS, FAIL, INCOMPLETE)
                ):
                    raise ValueError(
                        "Globalping 缺少有效总体状态"
                    )

            remote_by_identity = {
                _identity(result): result
                for result in remote_results
            }

            for original_index in eligible_indices:
                identity = _identity(local_results[original_index])
                output[original_index] = remote_by_identity[identity]

        except Exception as exc:
            reason = (
                f"Globalping 调用异常或结果校验失败：{exc}"
            )

            for original_index in eligible_indices:
                output[original_index] = globalping.incomplete_result(
                    local_results[original_index],
                    reason,
                )

    return [
        item
        if item is not None
        else globalping.incomplete_result(
            local_results[index],
            "检测结果未能完成交接。",
        )
        for index, item in enumerate(output)
    ]


# ----------------------------------------------------------------------
# 日志与人工来源诊断
# ----------------------------------------------------------------------

def _safe_host_from_url(value: Any) -> str:
    try:
        return urlsplit(str(value or "")).hostname or "无法识别"
    except ValueError:
        return "无法识别"


def _safe_host(candidate: dict[str, Any]) -> str:
    return _safe_host_from_url(
        candidate.get("normalized_url", "")
    )


def _safe_diagnostic_text(value: Any) -> str:
    text = str(value).replace("\r", " ").replace("\n", " ")
    pattern = re.compile(
        r"https?://[^\s<>\"']+",
        re.IGNORECASE,
    )

    def hide_url(match: re.Match[str]) -> str:
        raw = match.group(0)
        suffix = ""

        while raw and raw[-1] in ".,;)]}":
            suffix = raw[-1] + suffix
            raw = raw[:-1]

        return _safe_host_from_url(raw) + suffix

    return pattern.sub(hide_url, text)


def _print_detection_summary(
    results: list[dict[str, Any]],
) -> None:
    local_ok = sum(
        checker.local_checks_passed(item)
        for item in results
    )

    local_bad = len(results) - local_ok

    submitted_items = [
        item
        for item in results
        if checker.local_checks_passed(item)
    ]

    remote_counts = {
        state: 0
        for state in (PASS, FAIL, INCOMPLETE)
    }

    measurement_count = 0
    measurement_candidate_count = 0

    for item in submitted_items:
        remote = item.get("globalping", {})

        if not isinstance(remote, dict):
            continue

        if remote.get("status") in remote_counts:
            remote_counts[remote["status"]] += 1

        measurements = remote.get("measurements", [])

        if isinstance(measurements, list):
            valid_measurements = [
                measurement_id
                for measurement_id in measurements
                if isinstance(measurement_id, str)
                and measurement_id
            ]

            measurement_count += len(valid_measurements)

            if valid_measurements:
                measurement_candidate_count += 1

    print(
        f"检测统计：候选 {len(results)} 条；"
        f"本地全部通过 {local_ok} 条；"
        f"本地未通过/未完成 {local_bad} 条；"
        f"交给 Globalping {len(submitted_items)} 条候选；"
        f"返回结果记录测量 {measurement_count} 次；"
        f"有测量记录的候选 {measurement_candidate_count} 条；"
        f"无测量记录的远程候选 "
        f"{len(submitted_items) - measurement_candidate_count} 条；"
        f"本地未通过、未提交远程 {local_bad} 条；"
        f"Globalping 状态 PASS {remote_counts[PASS]} 条、"
        f"FAIL {remote_counts[FAIL]} 条、"
        f"INCOMPLETE {remote_counts[INCOMPLETE]} 条。"
    )

    for index, item in enumerate(submitted_items, 1):
        remote = item.get("globalping", {})

        if not isinstance(remote, dict):
            continue

        print(
            f"Globalping 明细 {index}/{len(submitted_items)}："
            f"频道 {item.get('channel', '未知频道')}；"
            f"类型 {item.get('media_type', '未知类型')}；"
            f"目标主机 {_safe_host(item)}；"
            f"总体状态 {remote.get('status', '未知')}。"
        )

        probes = remote.get("probes", {})

        if isinstance(probes, dict) and probes:
            for probe in probes.values():
                if isinstance(probe, dict):
                    print(
                        "  探针："
                        f"{probe.get('country', '未知地区')} "
                        f"{probe.get('city', '未知城市')}；"
                        f"网络 {probe.get('network', '未知网络')}；"
                        f"ASN {probe.get('asn', '未知')}。"
                    )
        else:
            print("  探针：没有取得有效探针结果。")

        stages = remote.get("stages", {})

        if isinstance(stages, dict) and stages:
            for stage_name, stage in stages.items():
                if not isinstance(stage, dict):
                    continue

                print(
                    f"  阶段 {stage_name}："
                    f"{stage.get('status', '未知')}；"
                    f"测量ID {stage.get('measurement_id') or '无'}；"
                    f"原因：{_safe_diagnostic_text(stage.get('reason') or '没有提供原因')}"
                )
        else:
            print("  阶段：没有取得阶段记录。")

        if remote.get("error"):
            print(
                "  远程异常/未完成原因："
                f"{_safe_diagnostic_text(remote['error'])}"
            )


def _print_manual_source_details(
    source: dict[str, Any],
    normalized: dict[str, Any],
    detected: list[dict[str, Any]],
    admission_results: list[dict[str, Any]],
) -> None:
    if source.get("source_type") != "manual":
        return

    candidates = normalized.get("candidates", [])

    print(
        f"人工来源诊断：{source.get('name', '未命名来源')}；"
        f"格式 {normalized.get('format', '未知')}；"
        f"提取 {normalized.get('extracted_candidates', 0)} 条；"
        f"匹配目标频道 {normalized.get('matched_target_candidates', 0)} 条；"
        f"重复 {normalized.get('duplicate_candidates', 0)} 条；"
        f"进入检测 {len(detected)} 条。"
    )

    for error in normalized.get("errors", []):
        print(
            f"  解析/匹配提示：{_safe_diagnostic_text(error)}"
        )

    admitted = {
        _identity(result.get("candidate", {})): result
        for result in admission_results
        if (
            isinstance(result, dict)
            and isinstance(result.get("candidate"), dict)
        )
    }

    detected_by_id = {
        _identity(result): result
        for result in detected
        if isinstance(result, dict)
    }

    for number, candidate in enumerate(candidates, 1):
        key = _identity(candidate)
        checked = detected_by_id.get(key, {})
        checks = checked.get("checks", {})
        remote = checked.get("globalping", {})
        admission = admitted.get(key, {})

        print(
            f"  [{number}] {candidate.get('channel', '未知频道')}；"
            f"主机 {_safe_host(candidate)}"
        )

        for check_name in ("ipv4", "http", "hls", "ts"):
            detail = checks.get(check_name, {})

            if isinstance(detail, dict):
                print(
                    f"      {check_name.upper()}："
                    f"{detail.get('status', '未执行')}；"
                    f"{_safe_diagnostic_text(detail.get('reason') or '无原因说明')}"
                )

        if isinstance(remote, dict):
            print(
                f"      Globalping：{remote.get('status', '未执行')}；"
                f"{_safe_diagnostic_text(remote.get('error') or '无异常说明')}"
            )

            stages = remote.get("stages", {})

            if isinstance(stages, dict):
                for stage_name, stage in stages.items():
                    if isinstance(stage, dict):
                        print(
                            f"        {stage_name}："
                            f"{stage.get('status', '未知')}；"
                            f"{_safe_diagnostic_text(stage.get('reason') or '无原因说明')}"
                        )

        print(
            f"      正式准入：{admission.get('status', '未进入准入')}；"
            f"{_safe_diagnostic_text(admission.get('reason') or '无原因说明')}"
        )


# ----------------------------------------------------------------------
# publisher 交接
# ----------------------------------------------------------------------

def process_admission_batch(
    candidates: list[dict[str, Any]],
    accepted_routes: list[dict[str, Any]],
    channels: list[str],
    settings: dict[str, Any],
) -> dict[str, Any]:
    return publisher.process_batch(
        candidates=candidates,
        accepted_routes=accepted_routes,
        channels=channels,
        settings=settings,
    )
    
# ----------------------------------------------------------------------
# 正式流程：先汇集全部来源，再统一检测、优选和准入
# ----------------------------------------------------------------------

def run_formal_flow(config: dict[str, Any]) -> int:
    settings = config["settings"]
    aliases = settings.get("aliases", {})

    if not isinstance(aliases, dict):
        raise ValueError("settings.json aliases 必须是字典")

    limits = settings["collection"]
    channels = config["channels"]

    seen_urls: set[str] = set()
    all_candidates: list[dict[str, Any]] = []
    source_records: list[
        tuple[dict[str, Any], dict[str, Any]]
    ] = []

    errors: list[str] = []

    valid_sources = 0
    extracted_total = 0
    duplicate_total = 0

    stopped = "所有配置来源均已处理"

    stream = iter_source_contents(
        config["manual_sources"],
        config["automatic_sources"],
        settings,
    )

    # 第一阶段：只采集、解析、归一和去重。
    # 此阶段不调用 checker、Globalping 或 publisher。
    for item in stream:
        source = item["source"]
        label = source.get(
            "name",
            source.get("url", "来源"),
        )

        if item.get("errors"):
            if source.get("source_type") == "manual":
                for message in item["errors"]:
                    print(
                        f"人工来源下载失败："
                        f"{source.get('name', '未命名来源')}；"
                        f"原因：{_safe_diagnostic_text(message)}"
                    )
            else:
                errors.extend(
                    f"{label}：{message}"
                    for message in item["errors"]
                )

            continue

        downloaded = item.get("download")

        if not isinstance(downloaded, dict):
            message = "采集器未返回下载内容"

            if source.get("source_type") == "manual":
                print(
                    f"人工来源采集异常：{label}；"
                    f"原因：{message}"
                )
            else:
                errors.append(f"{label}：{message}")

            continue

        content = downloaded.get("content")
        content_size = (
            len(content)
            if isinstance(content, (bytes, str))
            else 0
        )

        if source.get("source_type") == "manual":
            print(
                f"人工来源下载成功：{label}；"
                f"HTTP {downloaded.get('http_status', '未知')}；"
                f"内容 {content_size} 字节；"
                f"类型 {downloaded.get('content_type') or '未提供'}；"
                f"最终主机 {_safe_host_from_url(downloaded.get('final_url'))}。"
            )

        remaining = (
            limits["max_candidates_total"]
            - extracted_total
        )

        if remaining <= 0:
            stopped = (
                f"达到全部候选总上限 "
                f"{limits['max_candidates_total']}"
            )

            if source.get("source_type") == "manual":
                print(
                    f"人工来源停止解析：{label}；"
                    f"原因：{stopped}。"
                )

            break

        source_info = {
            **source,
            "final_url": downloaded["final_url"],
        }

        try:
            normalized = normalize_source_content(
                content=downloaded["content"],
                source=source_info,
                channels=channels,
                max_candidates_per_source=(
                    limits["max_candidates_per_source"]
                ),
                seen_urls=seen_urls,
                content_type=downloaded["content_type"],
                max_hls_manifest_bytes=(
                    settings["detection"]["max_hls_manifest_bytes"]
                ),
                aliases=aliases,
                max_candidates_remaining=remaining,
            )

        except Exception as exc:
            safe_message = _safe_diagnostic_text(exc)

            if source.get("source_type") == "manual":
                print(
                    f"人工来源解析异常：{label}；"
                    f"原因：{safe_message}"
                )
            else:
                errors.append(
                    f"{label}：归一解析异常：{safe_message}"
                )

            continue

        if not normalized.get("recognized"):
            _print_manual_source_details(
                source,
                normalized,
                [],
                [],
            )

            if source.get("source_type") != "manual":
                errors.extend(
                    f"{label}：{error}"
                    for error in normalized.get("errors", [])
                )

            continue

        extracted_total += normalized["extracted_candidates"]
        duplicate_total += normalized["duplicate_candidates"]

        candidates = normalized["candidates"]

        if normalized.get("matched_target_candidates", 0) > 0:
            valid_sources += 1

        all_candidates.extend(candidates)
        source_records.append((source, normalized))

        print(
            f"来源采集完成：{label}；"
            f"识别格式 {normalized['format']}；"
            f"提取候选 {normalized['extracted_candidates']} 条；"
            f"本来源新增候选 {len(candidates)} 条；"
            f"重复 {normalized['duplicate_candidates']} 条。"
        )

        if valid_sources >= limits["max_valid_sources"]:
            stopped = (
                f"达到有效来源上限 "
                f"{limits['max_valid_sources']}"
            )
            break

        if extracted_total >= limits["max_candidates_total"]:
            stopped = (
                f"达到全部候选总上限 "
                f"{limits['max_candidates_total']}"
            )
            break

    print(
        f"采集汇总：有效来源 {valid_sources}；"
        f"累计提取候选 {extracted_total}；"
        f"重复候选 {duplicate_total}；"
        f"进入统一检测候选池 {len(all_candidates)}。"
    )

    print(f"采集停止原因：{stopped}")

    for error in errors:
        print(
            f"来源警告：{error}",
            file=sys.stderr,
        )

    # 第二阶段：整个候选池只调用一次统一检测流程。
    try:
        detected = run_detection_pipeline(
            all_candidates,
            settings,
            channels=channels,
        )

    except Exception as exc:
        print(
            f"统一检测流程发生系统错误：{exc}",
            file=sys.stderr,
        )
        print(
            "正式订阅未修改。",
            file=sys.stderr,
        )
        return 1

    _print_detection_summary(detected)

    # 第三阶段：整轮只调用一次 publisher。
    # publisher 独占最终质量比较、正式准入和每频道最多两条保留，不按来源身份优先。
    try:
        admission = process_admission_batch(
            candidates=detected,
            accepted_routes=[],
            channels=channels,
            settings=settings,
        )

    except publisher.PublisherError as exc:
        print(
            f"线路准入系统错误：{exc}",
            file=sys.stderr,
        )
        print(
            "正式订阅未修改。",
            file=sys.stderr,
        )
        return 1

    accepted_routes = admission["accepted_routes"]
    admission_results = admission.get("results", [])

    accepted_count = admission["accepted_count"]
    rejected_count = admission["rejected_count"]

    print(
        f"正式准入汇总：候选 {len(detected)} 条；"
        f"本轮准入 {accepted_count} 条；"
        f"本轮拒绝 {rejected_count} 条；"
        f"最终保留 {len(accepted_routes)} 条。"
    )

    print(
        "频道有效线路统计："
        + "；".join(
            f"{channel} {count}/2"
            for channel, count in admission["channel_counts"].items()
        )
    )

    # 目标完成状态只读取 publisher 的正式返回结果。
    if admission["all_channels_reached"]:
        print(
            "本轮频道目标完成："
            "所有目标频道均达到每频道 2 条正式准入线路。"
        )
    else:
        print(
            "本轮频道目标未完成："
            "至少一个目标频道未达到每频道 2 条正式准入线路。"
        )

    # 统一检测和准入完成后，再输出人工来源逐条诊断。
    detected_by_id = {
        _identity(item): item
        for item in detected
        if isinstance(item, dict)
    }

    admission_by_id = {
        _identity(result.get("candidate", {})): result
        for result in admission_results
        if (
            isinstance(result, dict)
            and isinstance(result.get("candidate"), dict)
        )
    }

    for source, normalized in source_records:
        if source.get("source_type") != "manual":
            continue

        source_candidates = normalized.get("candidates", [])

        source_detected = [
            detected_by_id[_identity(candidate)]
            for candidate in source_candidates
            if _identity(candidate) in detected_by_id
        ]

        source_admission = [
            admission_by_id[_identity(candidate)]
            for candidate in source_candidates
            if _identity(candidate) in admission_by_id
        ]

        _print_manual_source_details(
            source,
            normalized,
            source_detected,
            source_admission,
        )

    # 第四阶段：生成并检查订阅内容。
    try:
        subscriptions = publisher.build_subscriptions(
            accepted_routes,
            channels,
        )

    except publisher.PublisherError as exc:
        print(
            f"订阅生成或格式检查失败：{exc}",
            file=sys.stderr,
        )
        print(
            "正式订阅未修改。",
            file=sys.stderr,
        )
        return 1

    print(
        f"订阅内容生成并校验通过："
        f"线路 {subscriptions['route_count']} 条；"
        f"频道 "
        f"{sum(count > 0 for count in subscriptions['channel_counts'].values())} 个。"
    )

    print(
        "本轮频道统计："
        + "；".join(
            f"{channel} {count}/2"
            for channel, count in subscriptions["channel_counts"].items()
        )
    )

    # 第五阶段：执行正式订阅安全发布。
    try:
        publish_result = publisher.publish_subscriptions(
            subscriptions,
            channels,
            output_dir=ROOT / "output",
        )

    except Exception as exc:
        print(
            f"正式订阅发布失败：{exc}",
            file=sys.stderr,
        )
        print(
            "请检查发布保护和回滚结果；"
            "本轮不能视为发布成功。",
            file=sys.stderr,
        )
        return 1

    print(
        f"正式订阅发布成功："
        f"频道 {publish_result['channel_count']} 个；"
        f"线路 {publish_result['route_count']} 条。"
    )

    print(f"M3U 文件：{publish_result['m3u_path']}")
    print(f"TXT 文件：{publish_result['txt_path']}")

    if publish_result["first_publish"]:
        print("本次属于首次发布。")

    return 0


# ----------------------------------------------------------------------
# 命令入口
# ----------------------------------------------------------------------

def parse_args(
    argv: list[str] | None = None,
) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="IPTV-Direct 自动订阅系统"
    )

    parser.add_argument(
        "--mode",
        required=True,
        choices=("formal", "test"),
        help="formal 正式运行；test 执行现有独立专项测试",
    )

    return parser.parse_args(argv)


def main(
    argv: list[str] | None = None,
) -> int:
    args = parse_args(argv)

    try:
        config = load_configuration(
            test_mode=args.mode == "test"
        )

    except ValueError as exc:
        print(
            f"配置检查失败：{exc}",
            file=sys.stderr,
        )
        return 1

    mode_name = (
        "正式运行"
        if args.mode == "formal"
        else "测试运行"
    )

    print(f"IPTV-Direct：{mode_name}")

    print(
        f"配置读取成功：目标频道 {len(config['channels'])} 个；"
        f"人工来源 {len(config['manual_sources'])} 条；"
        f"启用自动来源 {len(config['automatic_sources'])} 条。"
    )

    for error in config.get("errors", []):
        print(
            f"来源配置警告：{error}",
            file=sys.stderr,
        )

    if args.mode == "test":
        suite = unittest.defaultTestLoader.discover(
            str(ROOT / "tests")
        )

        result = unittest.TextTestRunner(
            verbosity=2
        ).run(suite)

        return 0 if result.wasSuccessful() else 1

    return run_formal_flow(config)


if __name__ == "__main__":
    raise SystemExit(main())
