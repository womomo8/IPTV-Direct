
"""IPTV-Direct：线路正式准入、频道统计和订阅内容生成。"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

PASS = "PASS"
FAIL = "FAIL"
INCOMPLETE = "INCOMPLETE"
NA = "N/A"

ACCEPTED = "ACCEPTED"
REJECTED = "REJECTED"

MAX_ROUTES_PER_CHANNEL = 2


class PublisherError(ValueError):
    """线路准入或订阅内容生成错误。"""


def _category_map(channels: list[str]) -> dict[str, str]:
    """读取 channels.txt 传递的唯一频道分类映射。"""
    mapping = getattr(channels, "category_by_channel", None)

    if mapping is None:
        return {}

    if not isinstance(mapping, dict):
        raise PublisherError("频道分类映射格式错误")

    if set(mapping) != set(channels):
        raise PublisherError("频道分类映射与目标频道列表不一致")

    return mapping


def _status(checks: Any, name: str) -> str | None:
    """读取单项检测状态。"""
    if not isinstance(checks, dict):
        return None

    item = checks.get(name)
    if not isinstance(item, dict):
        return None

    value = item.get("status")
    return value if isinstance(value, str) else None


def _valid_url(value: Any) -> bool:
    """确认候选 URL 是有效且已规范化的 HTTP/HTTPS 地址。"""
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(char.isspace() for char in value)
    ):
        return False

    try:
        parsed = urlsplit(value)
        scheme = parsed.scheme.lower()
        host = parsed.hostname
        port = parsed.port

        if (
            scheme not in ("http", "https")
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or parsed.netloc.endswith(":")
        ):
            return False

        if port is not None and not 1 <= port <= 65535:
            return False

        host = host.lower()

        if ":" in host:
            host = f"[{host}]"

        expected_netloc = host

        if port is not None and not (
            (scheme == "http" and port == 80)
            or (scheme == "https" and port == 443)
        ):
            expected_netloc += f":{port}"

        return (
            parsed.scheme == scheme
            and parsed.netloc == expected_netloc
            and bool(parsed.path)
            and parsed.path.startswith("/")
        )

    except (TypeError, ValueError):
        return False


def _required_checks(media_type: str) -> tuple[str, ...] | None:
    """根据实际识别的媒体类型返回适用检测项目。"""
    if media_type == "HLS":
        return ("ipv4", "http", "hls", "ts")

    if media_type == "DIRECT_TS":
        return ("ipv4", "http", "ts")

    return None


def _local_admission_reason(candidate: dict[str, Any]) -> str | None:
    """检查本地检测是否满足正式准入标准。"""
    media_type = candidate.get("media_type")
    required = _required_checks(media_type)

    if required is None:
        return "未知或不支持的媒体类型，不允许准入"

    checks = candidate.get("checks")

    if not isinstance(checks, dict):
        return "缺少本地检测结果"

    if media_type == "DIRECT_TS":
        if _status(checks, "hls") != NA:
            return "直接 TS 的 HLS 检测必须为 N/A"

    for name in required:
        state = _status(checks, name)

        if state != PASS:
            return f"本地 {name.upper()} 检测未通过：{state or '缺少结果'}"

    return None


def _prioritization_local_reason(
    candidate: dict[str, Any],
) -> str | None:
    """
    检查候选是否具备进入 Globalping 优选队列的本地资格。

    这是优选阶段独立的资格判断，不调用正式准入函数。
    优选阶段只负责筛选本地适用检测全部 PASS 的候选，
    不执行 Globalping 结果判定、正式准入或名额竞争。
    """
    media_type = candidate.get("media_type")
    required = _required_checks(media_type)

    if required is None:
        return "未知或不支持的媒体类型，不能进入 Globalping 优选队列"

    checks = candidate.get("checks")

    if not isinstance(checks, dict):
        return "缺少本地检测结果，不能进入 Globalping 优选队列"

    if media_type == "DIRECT_TS":
        if _status(checks, "hls") != NA:
            return "直接 TS 的 HLS 本地检测必须为 N/A"

    for name in required:
        state = _status(checks, name)

        if state != PASS:
            return (
                f"本地 {name.upper()} 检测未通过，"
                f"不能进入 Globalping 优选队列：{state or '缺少结果'}"
            )

    return None


def _globalping_admission_reason(
    candidate: dict[str, Any],
) -> str | None:
    """检查 Globalping 状态，允许符合规则的 INCOMPLETE 降级。"""
    remote = candidate.get("globalping")

    if not isinstance(remote, dict):
        return "缺少 Globalping 检测结果"

    overall = remote.get("status")

    if overall == FAIL:
        return "Globalping 检测明确失败"

    if overall not in (PASS, INCOMPLETE):
        return f"Globalping 总体状态无效：{overall}"

    media_type = candidate.get("media_type")
    required = _required_checks(media_type)

    if required is None:
        return "未知或不支持的媒体类型，不允许准入"

    checks = remote.get("checks")

    if not isinstance(checks, dict):
        return "缺少 Globalping 分项检测结果"

    if media_type == "DIRECT_TS":
        if _status(checks, "hls") != NA:
            return "Globalping 直接 TS 的 HLS 状态必须为 N/A"

    states = [_status(checks, name) for name in required]

    if any(state is None for state in states):
        return "Globalping 缺少适用检测项目的状态"

    if FAIL in states:
        return "Globalping 存在明确失败的检测项目"

    if overall == PASS and any(state != PASS for state in states):
        return "Globalping 总体 PASS 与分项检测结果不一致"

    if overall == INCOMPLETE and any(
        state not in (PASS, INCOMPLETE) for state in states
    ):
        return "Globalping INCOMPLETE 状态与分项检测结果不一致"

    return None


def _candidate_order(
    candidate: dict[str, Any],
) -> tuple[int, int]:
    """校验并读取来源优先级和来源内原始顺序元数据。"""
    priority = candidate.get("priority")
    order = candidate.get("source_order")

    if (
        isinstance(priority, bool)
        or not isinstance(priority, int)
        or priority < 0
    ):
        raise PublisherError("候选线路缺少有效来源优先级")

    if (
        isinstance(order, bool)
        or not isinstance(order, int)
        or order < 0
    ):
        raise PublisherError("候选线路缺少有效来源顺序")

    return priority, order


def _quality_number(
    quality: dict[str, Any],
    key: str,
) -> float | None:
    """读取有效质量数值，排除布尔值和无效类型。"""
    value = quality.get(key)

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None

    return float(value)


def _known_first(
    value: float | None,
    reverse: bool = False,
) -> tuple[int, float]:
    """已测数据优先；缺失数据不伪装为 0 或最好值。"""
    if value is None:
        return (1, 0.0)

    return (0, -value if reverse else value)


def _candidate_quality_key(
    candidate: dict[str, Any],
) -> tuple[Any, ...]:
    """
    使用 checker 已记录的真实质量数据分层比较。

    不计算综合分数，不改变 PASS/FAIL/INCOMPLETE 结果。
    该键只用于同频道、同媒体类型组内的稳定质量比较。
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


def _rank_routes(
    routes: list[dict[str, Any]],
    channels: list[str],
) -> list[dict[str, Any]]:
    """
    publisher 最终质量排序。

    只在同频道、同媒体类型内部比较质量。
    不同媒体类型不互相比较。
    质量相同或缺失时，稳定保持原始候选顺序。
    """
    channel_routes = {
        channel: []
        for channel in channels
    }

    for route in routes:
        channel = route.get("channel")

        if channel in channel_routes:
            channel_routes[channel].append(route)

    ranked = []

    for channel in channels:
        current = channel_routes[channel]

        positions_by_type: dict[Any, list[int]] = {}

        for index, route in enumerate(current):
            media_type = route.get("media_type")
            positions_by_type.setdefault(media_type, []).append(index)

        reordered = list(current)

        for positions in positions_by_type.values():
            same_type_routes = [
                current[index]
                for index in positions
            ]

            same_type_routes.sort(key=_candidate_quality_key)

            for index, route in zip(positions, same_type_routes):
                reordered[index] = route

        ranked.extend(reordered)

    return ranked


def _priority_key_for_audit(key: tuple[Any, ...]) -> list[dict[str, Any]]:
    """把分层质量键转换为可审计且不丢失未知状态的记录。"""
    return [
        {
            "known": part[0] == 0,
            "value": part[1] if part[0] == 0 else None,
        }
        for part in key
    ]


def _prioritize_candidates_for_globalping(
    candidates: list[dict[str, Any]],
    channels: list[str],
) -> dict[str, Any]:
    """构建完整优选队列；始终要求本地适用检测全部通过。"""
    if not isinstance(candidates, list):
        raise PublisherError("Globalping 优选输入必须是候选列表")

    if (
        not isinstance(channels, list)
        or any(not isinstance(channel, str) or not channel for channel in channels)
        or len(channels) != len(set(channels))
    ):
        raise PublisherError("Globalping 优选频道顺序无效")

    groups: dict[tuple[str, str], list[tuple[int, dict[str, Any]]]] = {}
    first_channel_position: dict[str, int] = {}
    first_group_position: dict[tuple[str, str], int] = {}
    excluded: list[dict[str, Any]] = []
    seen_identities: set[tuple[Any, Any]] = set()

    for original_index, candidate in enumerate(candidates):
        if not isinstance(candidate, dict):
            raise PublisherError(
                f"Globalping 优选输入第 {original_index + 1} 条不是候选对象"
            )

        channel = candidate.get("channel")
        url = candidate.get("normalized_url")
        media_type = candidate.get("media_type")

        if (
            not isinstance(channel, str)
            or not channel
            or not isinstance(url, str)
            or not url
        ):
            raise PublisherError(
                f"Globalping 优选输入第 {original_index + 1} 条缺少候选身份"
            )

        identity = (channel, url)

        if identity in seen_identities:
            raise PublisherError(
                f"Globalping 优选输入存在重复候选身份：{channel}"
            )

        seen_identities.add(identity)

        first_channel_position.setdefault(
            channel,
            len(first_channel_position),
        )

        if isinstance(media_type, str) and media_type:
            first_group_position.setdefault(
                (channel, media_type),
                original_index,
            )

        reason = _prioritization_local_reason(candidate)

        if reason is not None:
            excluded.append({
                "candidate": candidate,
                "identity": identity,
                "reason": reason,
            })
            continue

        group_key = (channel, media_type)
        groups.setdefault(group_key, []).append((original_index, candidate))

    configured_position = {
        channel: index
        for index, channel in enumerate(channels)
    }

    group_keys = sorted(
        groups,
        key=lambda key: (
            configured_position.get(key[0], len(channels)),
            (
                0
                if key[0] in configured_position
                else first_channel_position[key[0]] + 1
            ),
            first_group_position[key],
        ),
    )

    queues: list[
        tuple[tuple[str, str], list[tuple[int, dict[str, Any]]]]
    ] = []

    for group_key in group_keys:
        indexed_candidates = groups[group_key]

        ranked_group = sorted(
            indexed_candidates,
            key=lambda item: _candidate_quality_key(item[1]),
        )

        queues.append((group_key, ranked_group))

    queue: list[dict[str, Any]] = []
    ranking: list[dict[str, Any]] = []
    depth = 0

    while True:
        added = False

        for (channel, media_type), group in queues:
            if depth >= len(group):
                continue

            original_index, candidate = group[depth]
            quality_key = _candidate_quality_key(candidate)
            checks = candidate.get("checks")
            local_status = {
                name: _status(checks, name)
                for name in ("ipv4", "http", "hls", "ts")
            }

            queue.append(candidate)
            ranking.append({
                "identity": (channel, candidate["normalized_url"]),
                "original_index": original_index,
                "priority_order": len(queue),
                "group": {
                    "channel": channel,
                    "media_type": media_type,
                },
                "local_status": local_status,
                "quality_key": _priority_key_for_audit(quality_key),
                "quality_data": (
                    dict(candidate["quality"])
                    if isinstance(candidate.get("quality"), dict)
                    else {}
                ),
            })
            added = True

        if not added:
            break

        depth += 1

    return {
        "queue": queue,
        "excluded": excluded,
        "ranking": ranking,
    }


def prioritize_candidates_for_globalping(
    candidates: list[dict[str, Any]],
    channels: list[str],
) -> dict[str, Any]:
    """
    publisher 唯一公开的本地优选入口。

    只允许本地适用检测全部 PASS 的候选进入完整优选队列；
    返回完整候补、排除原因和逐项可复核的排序依据。
    """
    return _prioritize_candidates_for_globalping(
        candidates,
        channels,
    )


def _prepare_candidate(
    candidate: dict[str, Any],
    channels: list[str],
) -> tuple[dict[str, Any] | None, str | None]:
    """检查候选基础信息和正式准入条件。"""
    if not isinstance(candidate, dict):
        return None, "候选线路不是有效对象"

    channel = candidate.get("channel")

    if not isinstance(channel, str) or channel not in channels:
        return None, "频道名称不属于目标频道配置"

    url = candidate.get("normalized_url")

    if not _valid_url(url):
        return None, "缺少有效的规范化播放 URL"

    try:
        _candidate_order(candidate)
    except PublisherError as exc:
        return None, str(exc)

    reason = _local_admission_reason(candidate)

    if reason:
        return None, reason

    reason = _globalping_admission_reason(candidate)

    if reason:
        return None, reason

    result = dict(candidate)
    category_map = _category_map(channels)

    if category_map:
        result["category"] = category_map[channel]

    if "domestic_verification" not in result:
        result["domestic_verification"] = {
            "status": "NOT_VERIFIED",
            "reason": "本轮没有中国移动国内真实网络播放验证证据",
        }

    result["selection"] = {
        "status": ACCEPTED,
        "reason": (
            "本地适用检测全部 PASS，"
            f"Globalping 状态为 {result['globalping']['status']}"
        ),
    }

    return result, None


def _channel_counts(
    routes: list[dict[str, Any]],
    channels: list[str],
) -> dict[str, int]:
    """统计各目标频道累计准入线路数量。"""
    counts = {channel: 0 for channel in channels}

    for route in routes:
        channel = route.get("channel")

        if channel in counts:
            counts[channel] += 1

    return counts


def process_batch(
    candidates: list[dict[str, Any]],
    accepted_routes: list[dict[str, Any]],
    channels: list[str],
    settings: dict[str, Any],
) -> dict[str, Any]:
    """
    处理一批检测结果。

    输入：
      candidates：main.py 交来的完整检测结果。
      accepted_routes：上一批累计准入线路。
      channels：目标频道标准名称。
      settings：settings.json 完整配置。

    返回：
      results：本批逐线路准入结果。
      accepted_routes：累计保留的准入线路。
      channel_counts：各目标频道累计有效线路数量。
      all_channels_reached：是否全部达到线路数量目标。
    """
    if not isinstance(candidates, list):
        raise PublisherError("candidates 必须是列表")

    if not isinstance(accepted_routes, list):
        raise PublisherError("accepted_routes 必须是列表")

    if (
        not isinstance(channels, list)
        or any(not isinstance(item, str) or not item for item in channels)
    ):
        raise PublisherError("channels 必须是有效频道名称列表")

    if not isinstance(settings, dict):
        raise PublisherError("settings 必须是配置对象")

    selection = settings.get("selection")

    if not isinstance(selection, dict):
        raise PublisherError("settings 缺少 selection 配置")

    maximum = selection.get("max_routes_per_channel")

    if maximum != MAX_ROUTES_PER_CHANNEL:
        raise PublisherError(
            "每个频道最多保留线路数必须符合 MASTER_PLAN.md：2"
        )

    existing = []

    for route in accepted_routes:
        if not isinstance(route, dict):
            raise PublisherError("累计准入数据中存在无效线路")

        saved_route = dict(route)
        category_map = _category_map(channels)

        if category_map and saved_route.get("channel") in category_map:
            saved_route["category"] = category_map[saved_route["channel"]]

        existing.append(saved_route)

    seen_urls = {
        route.get("normalized_url")
        for route in existing
        if isinstance(route.get("normalized_url"), str)
    }

    prepared = []
    results = [None] * len(candidates)

    for candidate_index, candidate in enumerate(candidates):
        route, reason = _prepare_candidate(candidate, channels)

        if route is None:
            results[candidate_index] = {
                "candidate": candidate,
                "status": REJECTED,
                "reason": reason,
            }
            continue

        url = route["normalized_url"]

        if url in seen_urls:
            results[candidate_index] = {
                "candidate": candidate,
                "status": REJECTED,
                "reason": "线路 URL 已存在，不重复准入",
            }
            continue

        seen_urls.add(url)
        prepared.append((candidate_index, route))

    combined = _rank_routes(
        existing + [route for _, route in prepared],
        channels,
    )

    retained = []
    counts = {channel: 0 for channel in channels}
    retained_urls = set()

    for route in combined:
        channel = route.get("channel")
        url = route.get("normalized_url")

        if channel not in counts:
            continue

        if url in retained_urls:
            continue

        if counts[channel] >= MAX_ROUTES_PER_CHANNEL:
            continue

        retained.append(route)
        retained_urls.add(url)
        counts[channel] += 1

    existing_urls = {
        old.get("normalized_url")
        for old in existing
    }

    retained_new_urls = {
        route["normalized_url"]
        for route in retained
        if route["normalized_url"] not in existing_urls
    }

    for candidate_index, item in prepared:
        url = item["normalized_url"]

        if url in retained_new_urls:
            results[candidate_index] = {
                "candidate": item,
                "status": ACCEPTED,
                "reason": item["selection"]["reason"],
            }
        else:
            results[candidate_index] = {
                "candidate": item,
                "status": REJECTED,
                "reason": "该频道已达到最多保留 2 条线路的上限",
            }

    if any(result is None for result in results):
        raise PublisherError("本批候选结果生成不完整")

    all_reached = bool(channels) and all(
        counts[channel] >= MAX_ROUTES_PER_CHANNEL
        for channel in channels
    )

    accepted_count = sum(
        result.get("status") == ACCEPTED
        for result in results
    )

    rejected_count = sum(
        result.get("status") == REJECTED
        for result in results
    )

    return {
        "results": results,
        "accepted_routes": retained,
        "channel_counts": counts,
        "all_channels_reached": all_reached,
        "accepted_count": accepted_count,
        "rejected_count": rejected_count,
    }


def _safe_m3u_attribute(value: Any) -> str:
    """清理 M3U 属性值，避免破坏格式。"""
    if not isinstance(value, str):
        return ""

    value = value.replace("\r", " ").replace("\n", " ").strip()
    return value.replace('"', "&quot;")


def _valid_output_route(
    route: dict[str, Any],
    channels: list[str],
) -> bool:
    """检查最终输出线路。"""
    if not isinstance(route, dict):
        return False

    channel = route.get("channel")

    if channel not in channels:
        return False

    category_map = _category_map(channels)

    if category_map and route.get("category") != category_map[channel]:
        return False

    return (
        _valid_url(route.get("normalized_url"))
        and isinstance(route.get("selection"), dict)
        and route["selection"].get("status") == ACCEPTED
    )


def build_subscriptions(
    routes: list[dict[str, Any]],
    channels: list[str],
) -> dict[str, Any]:
    """
    使用同一份最终线路数据生成 M3U 和 TXT 文本。

    本函数只生成内容，不覆盖 output 中的正式文件。
    正式文件替换和失败回滚属于后续发布保护阶段。
    """
    if not isinstance(routes, list):
        raise PublisherError("routes 必须是列表")

    if not isinstance(channels, list) or not channels:
        raise PublisherError("没有有效目标频道")

    ordered = []

    for route in routes:
        if not _valid_output_route(route, channels):
            raise PublisherError("最终准入线路包含无效数据")

        ordered.append(route)

    channel_index = {
        channel: index
        for index, channel in enumerate(channels)
    }

    ordered.sort(
        key=lambda route: channel_index[route["channel"]]
    )

    counts = _channel_counts(ordered, channels)

    if any(value > MAX_ROUTES_PER_CHANNEL for value in counts.values()):
        raise PublisherError("最终线路数量超过每频道 2 条上限")

    urls = [route["normalized_url"] for route in ordered]

    if len(urls) != len(set(urls)):
        raise PublisherError("最终订阅存在重复 URL")

    m3u_lines = ["#EXTM3U"]
    txt_lines = []
    category_map = _category_map(channels)

    for route in ordered:
        channel = route["channel"]
        url = route["normalized_url"]

        category = (
            category_map[channel]
            if category_map
            else route.get("category", "")
        )

        if any(char in channel for char in ("\r", "\n", ",")):
            raise PublisherError("频道名称包含不支持的格式字符")

        attributes = []

        for key, attribute in (
            ("tvg_id", "tvg-id"),
            ("channel", "tvg-name"),
            ("category", "group-title"),
        ):
            raw_value = category if key == "category" else route.get(key)
            value = _safe_m3u_attribute(raw_value)

            if value:
                attributes.append(f'{attribute}="{value}"')

        suffix = (
            " " + " ".join(attributes)
            if attributes
            else ""
        )

        m3u_lines.append(f"#EXTINF:-1{suffix},{channel}")
        m3u_lines.append(url)

        txt_lines.append(f"{channel},{url}")

    m3u_text = "\n".join(m3u_lines) + "\n"
    txt_text = (
        "\n".join(txt_lines) + "\n"
        if txt_lines
        else ""
    )

    validate_subscriptions(m3u_text, txt_text, channels)

    return {
        "m3u": m3u_text,
        "txt": txt_text,
        "route_count": len(ordered),
        "channel_counts": counts,
    }


def validate_subscriptions(
    m3u_text: str,
    txt_text: str,
    channels: list[str],
) -> bool:
    """检查 M3U、TXT 格式及两份订阅内容一致性。"""
    if not isinstance(m3u_text, str) or not m3u_text:
        raise PublisherError("M3U 内容为空")

    if not isinstance(txt_text, str):
        raise PublisherError("TXT 内容格式无效")

    m3u_lines = m3u_text.splitlines()

    if not m3u_lines or m3u_lines[0] != "#EXTM3U":
        raise PublisherError("M3U 文件缺少 #EXTM3U 文件头")

    m3u_entries = []
    index = 1

    while index < len(m3u_lines):
        if index + 1 >= len(m3u_lines):
            raise PublisherError("M3U 存在不完整的频道条目")

        extinf = m3u_lines[index]
        url = m3u_lines[index + 1]

        if not extinf.startswith("#EXTINF:") or "," not in extinf:
            raise PublisherError("M3U 频道条目格式错误")

        channel = extinf.split(",", 1)[1].strip()

        if channel not in channels:
            raise PublisherError("M3U 包含非目标频道")

        category_map = _category_map(channels)

        if category_map:
            attributes = dict(
                re.findall(r'([\w-]+)="([^"]*)"', extinf)
            )
            expected_category = category_map[channel]

            if attributes.get("group-title") != expected_category:
                raise PublisherError(
                    f"M3U 频道分类错误：{channel}，"
                    f"要求 {expected_category}"
                )

        if not _valid_url(url):
            raise PublisherError("M3U 包含无效播放 URL")

        m3u_entries.append((channel, url))
        index += 2

    if not m3u_entries:
        raise PublisherError("订阅没有任何有效线路")

    txt_entries = []

    for line in txt_text.splitlines():
        if not line or "," not in line:
            raise PublisherError("TXT 订阅行格式错误")

        channel, url = line.split(",", 1)

        if channel not in channels:
            raise PublisherError("TXT 包含非目标频道")

        if not _valid_url(url):
            raise PublisherError("TXT 包含无效播放 URL")

        txt_entries.append((channel, url))

    if not txt_entries:
        raise PublisherError("TXT 订阅没有任何有效线路")

    if m3u_entries != txt_entries:
        raise PublisherError("M3U 与 TXT 的频道和线路内容不一致")

    urls = [url for _, url in m3u_entries]

    if len(urls) != len(set(urls)):
        raise PublisherError("订阅中存在重复 URL")

    counts = _channel_counts(
        [{"channel": channel} for channel, _ in m3u_entries],
        channels,
    )

    if any(value > MAX_ROUTES_PER_CHANNEL for value in counts.values()):
        raise PublisherError("订阅超过每频道 2 条线路上限")

    return True


def _subscription_snapshot(m3u_text, txt_text):
    """从旧订阅自身提取频道和线路数量，并检查两份文件一致性。"""
    if not isinstance(m3u_text, str) or not isinstance(txt_text, str):
        raise PublisherError("旧订阅不是有效文本")

    lines = m3u_text.splitlines()

    if not lines or lines[0] != "#EXTM3U":
        raise PublisherError("旧 M3U 缺少文件头")

    channels = []
    route_count = 0
    index = 1

    while index < len(lines):
        if index + 1 >= len(lines):
            raise PublisherError("旧 M3U 存在不完整条目")

        extinf = lines[index]
        url = lines[index + 1]

        if not extinf.startswith("#EXTINF:") or "," not in extinf:
            raise PublisherError("旧 M3U 条目格式错误")

        channel = extinf.split(",", 1)[1].strip()

        if not channel or not _valid_url(url):
            raise PublisherError("旧 M3U 包含无效频道或 URL")

        channels.append(channel)
        route_count += 1
        index += 2

    if not channels:
        raise PublisherError("旧订阅没有有效线路")

    unique_channels = list(dict.fromkeys(channels))

    validate_subscriptions(
        m3u_text,
        txt_text,
        unique_channels,
    )

    return {
        "channel_count": len(unique_channels),
        "route_count": route_count,
    }


def _write_temp_file(directory, prefix, data):
    """在正式文件所在目录创建临时文件。"""
    fd, filename = tempfile.mkstemp(
        prefix=prefix,
        suffix=".tmp",
        dir=str(directory),
    )

    path = Path(filename)

    try:
        with os.fdopen(fd, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise

    return path


def _restore_subscription_files(targets, old_data, directory):
    """逐个恢复旧文件，并记录所有恢复错误。"""
    errors = []

    for index, target in enumerate(targets):
        original = old_data[index]

        try:
            if original is None:
                target.unlink(missing_ok=True)
            else:
                restore_file = _write_temp_file(
                    directory,
                    f".{target.name}.restore-",
                    original,
                )

                try:
                    os.replace(restore_file, target)
                finally:
                    restore_file.unlink(missing_ok=True)

        except Exception as exc:
            errors.append(f"{target.name} 恢复失败：{exc}")

    return errors


def publish_subscriptions(
    subscriptions,
    channels,
    output_dir="output",
):
    """
    安全发布正式订阅。

    1. 检查新订阅。
    2. 检查旧订阅。
    3. 检查频道和线路数量下降保护。
    4. 写入临时文件。
    5. 替换正式文件。
    6. 重新读取并校验。
    7. 失败时恢复旧文件。
    """

    if not isinstance(subscriptions, dict):
        raise PublisherError("订阅数据格式错误")

    m3u_text = subscriptions.get("m3u")
    txt_text = subscriptions.get("txt")

    validate_subscriptions(
        m3u_text,
        txt_text,
        channels,
    )

    new_snapshot = _subscription_snapshot(
        m3u_text,
        txt_text,
    )

    if subscriptions.get("route_count") != new_snapshot["route_count"]:
        raise PublisherError("订阅线路数量统计不一致")

    directory = Path(output_dir)

    targets = [
        directory / "iptv.m3u",
        directory / "iptv.txt",
    ]

    directory.mkdir(parents=True, exist_ok=True)

    old_data = []

    for target in targets:
        try:
            data = target.read_bytes()
        except FileNotFoundError:
            data = None

        old_data.append(data)

    has_m3u = old_data[0] is not None
    has_txt = old_data[1] is not None

    if has_m3u != has_txt:
        raise PublisherError(
            "旧订阅只有一份存在有效内容，阻止发布"
        )

    both_old_files_empty = (
        old_data[0] == b""
        and old_data[1] == b""
    )

    first_publish = (
        (not has_m3u and not has_txt)
        or both_old_files_empty
    )

    if not first_publish:
        try:
            old_m3u = old_data[0].decode("utf-8-sig")
            old_txt = old_data[1].decode("utf-8-sig")

            old_snapshot = _subscription_snapshot(
                old_m3u,
                old_txt,
            )

        except (UnicodeDecodeError, PublisherError) as exc:
            raise PublisherError(
                f"旧订阅损坏，阻止覆盖：{exc}"
            ) from exc

        if (
            new_snapshot["channel_count"] * 2
            < old_snapshot["channel_count"]
        ):
            raise PublisherError(
                "频道数量下降超过50%，阻止发布"
            )

        if (
            new_snapshot["route_count"] * 2
            < old_snapshot["route_count"]
        ):
            raise PublisherError(
                "线路数量下降超过50%，阻止发布"
            )

    new_data = [
        m3u_text.encode("utf-8"),
        txt_text.encode("utf-8"),
    ]

    staged = []

    try:
        for target, data in zip(targets, new_data):
            staged.append(
                _write_temp_file(
                    directory,
                    f".{target.name}.stage-",
                    data,
                )
            )

        try:
            for temp_file, target in zip(staged, targets):
                os.replace(temp_file, target)

            actual_m3u = targets[0].read_text(
                encoding="utf-8-sig"
            )

            actual_txt = targets[1].read_text(
                encoding="utf-8-sig"
            )

            validate_subscriptions(
                actual_m3u,
                actual_txt,
                channels,
            )

            if actual_m3u != m3u_text or actual_txt != txt_text:
                raise PublisherError(
                    "正式文件内容与预期不一致"
                )

            actual_snapshot = _subscription_snapshot(
                actual_m3u,
                actual_txt,
            )

            if actual_snapshot != new_snapshot:
                raise PublisherError(
                    "正式文件数量核验失败"
                )

        except Exception as publish_error:
            rollback_errors = _restore_subscription_files(
                targets,
                old_data,
                directory,
            )

            if rollback_errors:
                raise PublisherError(
                    f"发布失败：{publish_error}；"
                    f"回滚失败：{'；'.join(rollback_errors)}"
                ) from publish_error

            raise PublisherError(
                f"发布失败，旧文件已恢复：{publish_error}"
            ) from publish_error

    finally:
        for temp_file in staged:
            temp_file.unlink(missing_ok=True)

    return {
        "published": True,
        "channel_count": new_snapshot["channel_count"],
        "route_count": new_snapshot["route_count"],
        "m3u_path": str(targets[0]),
        "txt_path": str(targets[1]),
        "first_publish": first_publish,
    }
