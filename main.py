
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IPTV-Direct 总入口：采集、检测、线路准入及订阅生成。"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unittest
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import checker
import globalping
import publisher
import stream

from collector import (
    SourceConfigError,
    iter_source_contents,
    prepare_sources,
)
from normalizer import (
    ChannelConfigError,
    load_channels,
    normalize_source_content,
)

ROOT = Path(__file__).resolve().parent
CONFIG_FILES = ("channels.txt", "manual.txt", "sources.json", "settings.json")

PASS = "PASS"
FAIL = "FAIL"
INCOMPLETE = "INCOMPLETE"


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
        raise ValueError("settings.json schedule.timezone 必须为 Asia/Shanghai")

    clock = settings["schedule"]["daily_time"]

    try:
        hour, minute = (int(part) for part in clock.split(":"))
    except (ValueError, AttributeError) as exc:
        raise ValueError(
            "settings.json schedule.daily_time 必须使用 HH:MM 格式"
        ) from exc

    if len(clock) != 5 or not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("settings.json schedule.daily_time 不是有效 HH:MM 时间")


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

    channel_names = {channel.casefold() for channel in channels}
    seen_aliases = set()

    for alias, canonical in aliases.items():
        if (
            not isinstance(alias, str)
            or not alias.strip()
            or not isinstance(canonical, str)
            or canonical.casefold() not in channel_names
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
# 本地 checker 与 Globalping 检测交接
# ----------------------------------------------------------------------

def _identity(item: dict[str, Any]) -> tuple[Any, Any]:
    return item.get("channel"), item.get("normalized_url")

def _load_historical_candidates(
    root: Path,
    channels: list[str],
    settings: dict[str, Any],
) -> list[dict[str, Any]]:
    """读取并归一化上一期订阅，将历史线路作为普通候选重新检测。"""

    output_dir = Path(root) / "output"
    m3u_path = output_dir / "iptv.m3u"
    txt_path = output_dir / "iptv.txt"

    has_m3u = m3u_path.is_file()
    has_txt = txt_path.is_file()

    # 首次发布：两份历史文件都不存在。
    if not has_m3u and not has_txt:
        return []

    # 只存在一份文件时，不能静默忽略历史线路。
    if has_m3u != has_txt:
        raise publisher.PublisherError(
            "历史订阅 M3U 和 TXT 文件不完整，无法确认历史线路"
        )

    try:
        m3u_text = m3u_path.read_text(encoding="utf-8-sig")
        txt_text = txt_path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise publisher.PublisherError(
            f"历史订阅读取失败：{exc}"
        ) from exc

    # 先确认旧订阅两份文件一致、频道有效、URL 有效。
    try:
        publisher.validate_subscriptions(
            m3u_text,
            txt_text,
            channels,
        )
    except publisher.PublisherError as exc:
        raise publisher.PublisherError(
            f"历史订阅校验失败：{exc}"
        ) from exc

    route_count = sum(
        line.startswith("#EXTINF:")
        for line in m3u_text.splitlines()
    )

    limits = settings["collection"]

    source = {
        "name": "历史订阅",
        "url": "history://output/iptv.m3u",
        "source_type": "history",
        "priority": 1,
        # 同质量时不让历史身份获得排名优势。
        "order": limits["max_candidates_total"],
    }

    try:
        normalized = normalize_source_content(
            content=m3u_text,
            source=source,
            channels=channels,
            max_candidates_per_source=limits["max_candidates_total"],
            seen_urls=set(),
            content_type="application/vnd.apple.mpegurl",
            max_hls_manifest_bytes=(
                settings["detection"]["max_hls_manifest_bytes"]
            ),
            aliases=settings.get("aliases", {}),
            max_candidates_remaining=limits["max_candidates_total"],
            channels_path=str(Path(root) / "channels.txt"),
        )
    except Exception as exc:
        raise publisher.PublisherError(
            f"历史线路解析失败：{exc}"
        ) from exc

    candidates = normalized.get("candidates", [])

    # 必须确保旧订阅中的每一条线路都成功进入候选池。
    if (
        not normalized.get("recognized")
        or normalized.get("limit_reached")
        or len(candidates) != route_count
    ):
        raise publisher.PublisherError(
            "历史订阅线路解析数量不完整，不能跳过未解析线路"
        )

    return candidates

def run_source_batch_pipeline(
    candidates: list[dict[str, Any]],
    accepted_routes: list[dict[str, Any]],
    settings: dict[str, Any],
    channels: list[str],
) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    """完成整批本地与视频检测、逐条 Globalping，最后统一交 publisher 准入。"""

    if not candidates:
        admission = publisher.process_batch(
            [], accepted_routes, channels, settings
        )
        return [], admission, []

    if not isinstance(channels, list) or any(
        not isinstance(channel, str) or not channel.strip()
        for channel in channels
    ):
        raise ValueError("Globalping 交接必须提供有效的目标频道列表")

    target_channels = {channel.casefold() for channel in channels}
    originals = [_identity(item) for item in candidates]

    try:
        local_results = checker.check_candidates(
            candidates, settings["detection"]
        )
        if (
            not isinstance(local_results, list)
            or len(local_results) != len(candidates)
            or any(
                not isinstance(item, dict)
                or _identity(item) != originals[index]
                for index, item in enumerate(local_results)
            )
        ):
            raise ValueError("checker 返回数量、顺序或候选身份不匹配")
    except Exception as exc:
        reason = f"本地检测调用异常或结果校验失败：{exc}"
        local_results = [
            {
                **item,
                "media_type": "UNKNOWN",
                "checks": {
                    key: {"status": INCOMPLETE, "reason": reason}
                    for key in ("ipv4", "http", "hls", "ts")
                },
            }
            for item in candidates
        ]
    stream_indices = [
        index
        for index, item in enumerate(local_results)
        if (
            isinstance(item.get("channel"), str)
            and item["channel"].casefold() in target_channels
            and checker.local_checks_passed(item)
        )
    ]

    stream_candidates = [
        local_results[index] for index in stream_indices
    ]

    if stream_candidates:
        try:
            stream_results = stream.check_candidates(stream_candidates)

            if (
                not isinstance(stream_results, list)
                or len(stream_results) != len(stream_candidates)
                or any(
                    not isinstance(item, dict)
                    or _identity(item) != _identity(stream_candidates[position])
                    for position, item in enumerate(stream_results)
                )
            ):
                raise ValueError("stream 返回数量、顺序或候选身份不匹配")

            for index, result in zip(stream_indices, stream_results):
                local_results[index] = result

        except Exception as exc:
            reason = f"视频质量检测调用异常或结果校验失败：{exc}"

            for index in stream_indices:
                local_results[index] = {
                    **local_results[index],
                    "stream_quality": {
                        "status": INCOMPLETE,
                        "reason": reason,
                    },
                }
    output: list[dict[str, Any] | None] = [None] * len(candidates)
    pending: list[tuple[int, dict[str, Any]]] = []

    for index, item in enumerate(local_results):
        channel = item.get("channel")

        if (
            not isinstance(channel, str)
            or channel.casefold() not in target_channels
        ):
            output[index] = globalping.incomplete_result(
                item,
                "候选频道不在 channels.txt 目标频道列表中，未提交 Globalping。",
            )
        elif not checker.local_checks_passed(item):
            output[index] = globalping.incomplete_result(
                item,
                "本地检测未全部通过，未提交 Globalping；不代表远程检测失败。",
            )
        elif not stream.quality_passed(item):
            output[index] = globalping.incomplete_result(
                item,
                "视频质量检测未明确通过，未提交 Globalping。",
            )
        else:
            pending.append((index, item))
    # 按来源轮流提交 Globalping，避免某一个来源先耗尽检测额度。
    # 同一来源内部保持原始候选顺序；检测结果仍按原下标回填。
    pending_by_source = {}

    for original_index, candidate in pending:
        source_type = candidate.get("source_type")
        source_url = candidate.get("source_url")

        if (
            not isinstance(source_type, str)
            or not source_type
            or not isinstance(source_url, str)
            or not source_url
        ):
            source_key = ("candidate", str(original_index))
        else:
            source_key = (source_type, source_url)

        pending_by_source.setdefault(source_key, []).append(
            (original_index, candidate)
        )

    fair_pending = []

    while pending_by_source:
        for source_key in list(pending_by_source):
            source_queue = pending_by_source[source_key]
            fair_pending.append(source_queue.pop(0))

            if not source_queue:
                del pending_by_source[source_key]

    pending = fair_pending

    # 所有通过本地检测和视频质量检测的候选，
    # 都必须逐条完成 Globalping 检测。
    # 不得根据频道当前已有 PASS 数量提前停止。

    pending.sort(key=_globalping_dispatch_order)
    # 所有通过本地检测和视频质量检测的候选，
    # 都必须逐条完成 Globalping 检测。
    # 不得根据频道当前已有 PASS 数量提前停止。
    submitted_candidates: list[dict[str, Any]] = []

    session = None
    session_error = None
    session_created = False

    for original_index, candidate in pending:

        if not session_created:
            session_created = True
            try:
                session = globalping.create_session(
                    settings["globalping"]
                )
            except Exception as exc:
                session_error = f"Globalping 会话初始化异常：{exc}"

        if session_error:
            result = globalping.incomplete_result(
                candidate, session_error
            )
        else:
            try:
                remote_results = globalping.check_candidates(
                    [candidate],
                    settings["globalping"],
                    session=session,
                )

                if (
                    not isinstance(remote_results, list)
                    or len(remote_results) != 1
                ):
                    raise ValueError(
                        "Globalping 返回数量与单条候选不一致"
                    )

                result = remote_results[0]

                if (
                    not isinstance(result, dict)
                    or _identity(result) != _identity(candidate)
                ):
                    raise ValueError(
                        "Globalping 返回候选身份不匹配"
                    )

                remote = result.get("globalping")
                if (
                    not isinstance(remote, dict)
                    or remote.get("status") not in (
                        PASS, FAIL, INCOMPLETE
                    )
                ):
                    raise ValueError(
                        "Globalping 缺少有效总体状态"
                    )

            except Exception as exc:
                result = globalping.incomplete_result(
                    candidate,
                    f"Globalping 调用异常或结果校验失败：{exc}",
                )

        output[original_index] = result

        remote = result.get("globalping", {})
        measurements = (
            remote.get("measurements", [])
            if isinstance(remote, dict)
            else []
        )
        stages = (
            remote.get("stages", {})
            if isinstance(remote, dict)
            else {}
        )

        has_measurement = (
            isinstance(measurements, list)
            and bool(measurements)
        ) or (
            isinstance(stages, dict)
            and any(
                isinstance(stage, dict)
                and stage.get("measurement_id")
                for stage in stages.values()
            )
        )

        if has_measurement:
            submitted_candidates.append(result)

    completed_results = []

    for index, result in enumerate(output):
        if not isinstance(result, dict):
            raise publisher.PublisherError(
                f"第 {index + 1} 条候选没有取得完整检测结果"
            )
        completed_results.append(result)

    # 所有候选完成本地、视频和适用的 Globalping 检测后，
    # 才统一交给 publisher 执行最终准入。
    admission = publisher.process_batch(
        completed_results,
        accepted_routes,
        channels,
        settings,
    )

    returned = admission.get("results")

    if not isinstance(returned, list) or len(returned) != len(candidates):
        raise publisher.PublisherError(
            "publisher 未完整返回全局候选的准入结果"
        )

    index_by_identity: dict[
        tuple[Any, Any], list[int]
    ] = {}

    for index, candidate in enumerate(candidates):
        index_by_identity.setdefault(
            _identity(candidate), []
        ).append(index)

    admission_by_index: dict[int, dict[str, Any]] = {}

    for item in returned:
        if not isinstance(item, dict):
            raise publisher.PublisherError(
                "publisher 返回了无效准入结果"
            )

        candidate = item.get("candidate")

        if not isinstance(candidate, dict):
            raise publisher.PublisherError(
                "publisher 返回了无效候选身份"
            )

        matches = index_by_identity.get(_identity(candidate), [])

        if not matches:
            raise publisher.PublisherError(
                "publisher 返回了不属于本批候选的准入结果"
            )

        admission_by_index[matches.pop(0)] = item

    if len(admission_by_index) != len(candidates):
        raise publisher.PublisherError(
            "全局候选没有全部取得 publisher 准入结果"
        )

    admission = dict(admission)
    admission["results"] = [
        admission_by_index[index]
        for index in range(len(candidates))
    ]

    return (
        completed_results,
        admission,
        submitted_candidates,
    )

def _print_detection_summary(
    results: list[dict[str, Any]],
    submitted_candidates: list[dict[str, Any]] | None = None,
) -> None:
    """汇总本地检测结果，以及真正提交给 Globalping 的检测明细。"""

    local_ok = sum(checker.local_checks_passed(item) for item in results)
    local_bad = len(results) - local_ok
    submitted_items = (
        submitted_candidates
        if submitted_candidates is not None
        else [
            item for item in results
            if checker.local_checks_passed(item)
        ]
    )

    remote_counts = {state: 0 for state in (PASS, FAIL, INCOMPLETE)}
    for item in submitted_items:
        remote = item.get("globalping", {})
        if isinstance(remote, dict) and remote.get("status") in remote_counts:
            remote_counts[remote["status"]] += 1

    print(
        f"检测统计：候选 {len(results)} 条；"
        f"本地全部通过 {local_ok} 条；"
        f"本地未通过/未完成 {local_bad} 条；"
        f"Globalping 已提交 {len(submitted_items)} 条、"
        f"未提交 {len(results) - len(submitted_items)} 条；"
        f"实际远程结果 PASS {remote_counts[PASS]} 条、"
        f"FAIL {remote_counts[FAIL]} 条、"
        f"INCOMPLETE {remote_counts[INCOMPLETE]} 条。"
    )

    stream_counts = {state: 0 for state in (PASS, FAIL, INCOMPLETE)}
    stream_checked = 0

    for index, item in enumerate(results, 1):
        quality = item.get("stream_quality")
        if not isinstance(quality, dict):
            continue

        stream_checked += 1
        status = quality.get("status", "UNKNOWN")
        if status in stream_counts:
            stream_counts[status] += 1

        width = quality.get("width")
        height = quality.get("height")
        resolution = (
            f"{width}×{height}"
            if isinstance(width, int) and isinstance(height, int)
            else "无法确认"
        )

        print(
            f"视频质量明细 {index}/{len(results)}："
            f"频道 {item.get('channel', '未知频道')}；"
            f"结果 {status}；"
            f"分辨率 {resolution}；"
            f"扫描方式 {quality.get('field_order') or '未知'}；"
            f"编码 {quality.get('codec') or '未知'}；"
            f"原因：{_safe_diagnostic_text(quality.get('reason') or '未提供')}"
        )

    print(
        f"视频质量统计：实际检测 {stream_checked} 条；"
        f"PASS {stream_counts[PASS]} 条；"
        f"FAIL {stream_counts[FAIL]} 条；"
        f"INCOMPLETE {stream_counts[INCOMPLETE]} 条；"
        f"未进入视频检测 {len(results) - stream_checked} 条。"
    )    
    
    for index, item in enumerate(submitted_items, 1):
        remote = item.get("globalping", {})
        if not isinstance(remote, dict):
            continue

        channel = item.get("channel", "未知频道")
        media_type = item.get("media_type", "未知类型")
        try:
            host = urlsplit(str(item.get("normalized_url", ""))).hostname or "无法识别"
        except ValueError:
            host = "无法识别"

        print(
            f"Globalping 明细 {index}/{len(submitted_items)}："
            f"频道 {channel}；类型 {media_type}；目标主机 {host}；"
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


def _safe_host_from_url(value: Any) -> str:
    """从 URL 提取主机名，不输出路径、查询参数或凭据。"""
    try:
        return urlsplit(str(value or "")).hostname or "无法识别"
    except ValueError:
        return "无法识别"


def _safe_host(candidate: dict[str, Any]) -> str:
    """只显示主机名，避免日志泄露 URL 参数或凭据。"""
    return _safe_host_from_url(candidate.get("normalized_url", ""))


def _safe_diagnostic_text(value: Any) -> str:
    """保留错误原因，同时将其中的完整 URL 替换为主机名。"""
    text = str(value).replace("\r", " ").replace("\n", " ")
    pattern = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)

    def hide_url(match: re.Match[str]) -> str:
        raw = match.group(0)
        suffix = ""
        while raw and raw[-1] in ".,;)]}":
            suffix = raw[-1] + suffix
            raw = raw[:-1]
        return _safe_host_from_url(raw) + suffix

    return pattern.sub(hide_url, text)


def _print_manual_source_details(
    source: dict[str, Any],
    normalized: dict[str, Any],
    detected: list[dict[str, Any]],
    admission_results: list[dict[str, Any]],
) -> None:
    """仅输出 manual.txt 来源的逐条采集、检测和准入结果。"""
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
        print(f"  解析/匹配提示：{_safe_diagnostic_text(error)}")

    admitted = {
        _identity(result.get("candidate", {})): result
        for result in admission_results
        if isinstance(result, dict) and isinstance(result.get("candidate"), dict)
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
                            f"        {stage_name}：{stage.get('status', '未知')}；"
                            f"{_safe_diagnostic_text(stage.get('reason') or '无原因说明')}"
                        )
        print(
            f"      正式准入：{admission.get('status', '未进入准入')}；"
            f"{_safe_diagnostic_text(admission.get('reason') or '无原因说明')}"
        )


# ----------------------------------------------------------------------
# publisher 交接与停止条件
# ----------------------------------------------------------------------

def process_admission_batch(
    candidates: list[dict[str, Any]],
    accepted_routes: list[dict[str, Any]],
    channels: list[str],
    settings: dict[str, Any],
) -> dict[str, Any]:
    """将本批完整检测结果交给 publisher 执行正式准入。"""

    return publisher.process_batch(
        candidates=candidates,
        accepted_routes=accepted_routes,
        channels=channels,
        settings=settings,
    )

def _collection_stop_reason(
    valid_sources: int,
    extracted_total: int,
    admission: dict[str, Any],
    limits: dict[str, Any],
) -> str | None:
    """只根据采集资源上限判断是否停止，不以当前线路数量停止。"""

    if valid_sources >= limits["max_valid_sources"]:
        return f"达到有效来源上限 {limits['max_valid_sources']}"

    if extracted_total >= limits["max_candidates_total"]:
        return f"达到全部候选总上限 {limits['max_candidates_total']}"

    return None


# ----------------------------------------------------------------------
# 正式流程：逐来源采集、检测、准入和订阅生成
# ----------------------------------------------------------------------

def run_formal_flow(config: dict[str, Any]) -> int:
    settings = config["settings"]
    aliases = settings.get("aliases", {})
    if not isinstance(aliases, dict):
        raise ValueError("settings.json aliases 必须是字典")

    limits = settings["collection"]
    channels = config["channels"]

    try:
        history_candidates = _load_historical_candidates(
            ROOT,
            channels,
            settings,
        )
    except publisher.PublisherError as exc:
        print(
            f"历史订阅读取或校验失败，停止本轮处理：{exc}",
            file=sys.stderr,
        )
        print("正式订阅未修改。", file=sys.stderr)
        return 1   
    
    seen_urls: set[str] = set()
    errors: list[str] = []
    detected: list[dict[str, Any]] = []
    all_candidates: list[dict[str, Any]] = []
    source_records: list[dict[str, Any]] = []

    accepted_routes: list[dict[str, Any]] = []
    admission = {
        "accepted_routes": [],
        "channel_counts": {channel: 0 for channel in channels},
        "all_channels_reached": False,
    }

    valid_sources = 0
    extracted_total = len(history_candidates)
    duplicate_total = 0
    stopped = "所有配置来源均已处理"
    system_error = False

    stream = iter_source_contents(
        config["manual_sources"],
        config["automatic_sources"],
        settings,
    )

    for item in stream:
        source = item["source"]
        label = source.get("name", source.get("url", "来源"))

        if item.get("errors"):
            if source.get("source_type") == "manual":
                for message in item["errors"]:
                    print(
                        f"人工来源下载失败：{source.get('name', '未命名来源')}；"
                        f"原因：{_safe_diagnostic_text(message)}"
                    )
            else:
                errors.extend(
                    f"{label}：{message}" for message in item["errors"]
                )
            continue

        downloaded = item.get("download")

        if not isinstance(downloaded, dict):
            message = "采集器未返回下载内容"
            if source.get("source_type") == "manual":
                print(f"人工来源采集异常：{label}；原因：{message}")
            else:
                errors.append(f"{label}：{message}")
            continue

        content = downloaded.get("content")
        content_size = len(content) if isinstance(content, (bytes, str)) else 0
        if source.get("source_type") == "manual":
            print(
                f"人工来源下载成功：{label}；"
                f"HTTP {downloaded.get('http_status', '未知')}；"
                f"内容 {content_size} 字节；"
                f"类型 {downloaded.get('content_type') or '未提供'}；"
                f"最终主机 {_safe_host_from_url(downloaded.get('final_url'))}。"
            )

        remaining = limits["max_candidates_total"] - extracted_total

        if remaining <= 0:
            stopped = f"达到全部候选总上限 {limits['max_candidates_total']}"
            if source.get("source_type") == "manual":
                print(f"人工来源停止解析：{label}；原因：{stopped}。")
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
                max_candidates_per_source=limits["max_candidates_per_source"],
                seen_urls=seen_urls,
                content_type=downloaded["content_type"],
                max_hls_manifest_bytes=(
                    settings["detection"]["max_hls_manifest_bytes"]
                ),
                aliases=aliases,
                max_candidates_remaining=remaining,
                channels_path=str(ROOT / "channels.txt"),
            )
        except Exception as exc:
            safe_message = _safe_diagnostic_text(exc)
            if source.get("source_type") == "manual":
                print(f"人工来源解析异常：{label}；原因：{safe_message}")
            else:
                errors.append(f"{label}：归一解析异常：{safe_message}")
            continue

        if not normalized.get("recognized"):
            _print_manual_source_details(source, normalized, [], [])
            if source.get("source_type") != "manual":
                errors.extend(
                    f"{label}：{error}"
                    for error in normalized.get("errors", [])
                )
            continue

        extracted_total += normalized["extracted_candidates"]
        duplicate_total += normalized["duplicate_candidates"]

        candidates = normalized["candidates"]

        if normalized.get("extracted_candidates", 0) > 0:
            valid_sources += 1

        if candidates:
            all_candidates.extend(candidates)
            source_records.append({
                "source": source,
                "normalized": normalized,
                "candidates": candidates,
            })
        else:
            _print_manual_source_details(
                source, normalized, [], []
            )

        print(
            f"来源处理：{label}；"
            f"识别格式 {normalized['format']}；"
            f"提取候选 {normalized['extracted_candidates']}；"
            f"本批去重 {normalized['duplicate_candidates']}；"
            f"进入全局候选池 {len(candidates)}。"
        )

        stop_reason = _collection_stop_reason(
            valid_sources,
            extracted_total,
            admission,
            limits,
        )

        if stop_reason:
            stopped = stop_reason
            break

     # 历史线路必须重新检测，不能直接作为已准入线路。
    history_added = 0

    for candidate in history_candidates:
        url = candidate["normalized_url"]

        # 同一 URL 只保留一个候选。
        # 如果新来源已经提供该线路，以新来源候选为准。
        if url in seen_urls:
            duplicate_total += 1
            continue

        seen_urls.add(url)
        all_candidates.append(candidate)
        history_added += 1

    if history_candidates:
        print(
            f"历史订阅重新加入本轮检测：{history_added} 条；"
            f"与新来源重复 {len(history_candidates) - history_added} 条。"
        )   
    
    if all_candidates:
        try:
            (
                batch_results,
                admission,
                submitted_candidates,
            ) = run_source_batch_pipeline(
                candidates=all_candidates,
                accepted_routes=accepted_routes,
                settings=settings,
                channels=channels,
            )
        except publisher.PublisherError as exc:
            errors.append(f"线路准入系统错误：{exc}")
            stopped = "publisher 正式准入发生系统错误"
            system_error = True
        else:
            detected.extend(batch_results)

            _print_detection_summary(
                batch_results,
                submitted_candidates,
            )

            accepted_routes = admission["accepted_routes"]
            admission_results = admission["results"]

            for source_record in source_records:
                identities = {
                    _identity(candidate)
                    for candidate in source_record["candidates"]
                }

                source_batch_results = [
                    result
                    for result in batch_results
                    if _identity(result) in identities
                ]

                source_admission_results = [
                    result
                    for result in admission_results
                    if (
                        isinstance(result, dict)
                        and isinstance(result.get("candidate"), dict)
                        and _identity(result["candidate"]) in identities
                    )
                ]

                _print_manual_source_details(
                    source_record["source"],
                    source_record["normalized"],
                    source_batch_results,
                    source_admission_results,
                )

            accepted_now = sum(
                result.get("status") == publisher.ACCEPTED
                for result in admission_results
            )
            rejected_now = sum(
                result.get("status") == publisher.REJECTED
                for result in admission_results
            )

            print(
                f"全局准入：本轮准入 {accepted_now} 条；"
                f"本轮拒绝 {rejected_now} 条；"
                f"最终保留 {len(accepted_routes)} 条。"
            )

            print(
                "频道有效线路统计："
                + "；".join(
                    f"{channel} {count}/2"
                    for channel, count in admission["channel_counts"].items()
                )
            )    
    
    print(
        f"来源汇总：有效来源 {valid_sources}；"
        f"累计提取候选 {extracted_total}；"
        f"重复候选 {duplicate_total}；"
        f"实际完成检测 {len(detected)}。"
    )
    print(f"采集停止原因：{stopped}")

    for error in errors:
        print(f"来源警告：{error}", file=sys.stderr)

    if system_error:
        print(
            "发生准入系统错误，停止后续处理；"
            "正式订阅未修改。",
            file=sys.stderr,
        )
        return 1

    # 第8阶段：生成并检查订阅内容。
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
        print("正式订阅未修改。", file=sys.stderr)
        return 1

    print(
        f"订阅内容生成并校验通过："
        f"线路 {subscriptions['route_count']} 条；"
        f"频道 {sum(count > 0 for count in subscriptions['channel_counts'].values())} 个。"
    )

    print(
        "本轮频道统计："
        + "；".join(
            f"{channel} {count}/2"
            for channel, count in subscriptions["channel_counts"].items()
        )
    )

    print(
        "第8阶段处理完成：已执行正式准入、频道统计、"
        "M3U/TXT 生成及一致性检查。"
    )

    # 第9阶段：执行正式订阅安全发布。
    try:
        publish_result = publisher.publish_subscriptions(
            subscriptions,
            channels,
            output_dir=ROOT / "output",
        )
    except Exception as exc:
        print(
            f"第9阶段正式订阅发布失败：{exc}",
            file=sys.stderr,
        )
        print(
            "请检查发布保护和回滚结果；"
            "本轮不能视为发布成功。",
            file=sys.stderr,
        )
        return 1

    print(
        "第9阶段正式订阅发布成功："
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

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
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


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        config = load_configuration(
            test_mode=args.mode == "test"
        )
    except ValueError as exc:
        print(f"配置检查失败：{exc}", file=sys.stderr)
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
        print(f"来源配置警告：{error}", file=sys.stderr)

    if args.mode == "test":
        suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        return 0 if result.wasSuccessful() else 1

    return run_formal_flow(config)


if __name__ == "__main__":
    raise SystemExit(main())
