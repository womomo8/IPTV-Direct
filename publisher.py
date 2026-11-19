
"""IPTV-Direct：本地线路质量比较、分组与完整优选队列。"""

from __future__ import annotations

import math
from typing import Any

PASS = "PASS"
NA = "N/A"


class PublisherError(ValueError):
    """Publisher 本地优选输入或处理错误。"""


def _status(checks: Any, name: str) -> str | None:
    """读取本地单项检测状态。"""
    if not isinstance(checks, dict):
        return None

    item = checks.get(name)
    if not isinstance(item, dict):
        return None

    value = item.get("status")
    return value if isinstance(value, str) else None


def _local_exclusion_reason(
    candidate: dict[str, Any],
) -> str | None:
    """只有本地适用检测全部 PASS 的候选才可进入优选池。"""
    media_type = candidate.get("media_type")
    checks = candidate.get("checks")

    if media_type == "HLS":
        required = ("ipv4", "http", "hls", "ts")

    elif media_type == "DIRECT_TS":
        required = ("ipv4", "http", "ts")

        if _status(checks, "hls") != NA:
            return "直接 TS 的 HLS 本地检测必须为 N/A"

    else:
        return "未知或不支持的媒体类型，不能进入本地优选队列"

    if not isinstance(checks, dict):
        return "缺少本地检测结果"

    for name in required:
        state = _status(checks, name)

        if state != PASS:
            return (
                f"本地 {name.upper()} 检测未通过，"
                f"不能进入优选队列：{state or '缺少结果'}"
            )

    return None


def _quality_number(
    quality: dict[str, Any],
    key: str,
) -> float | None:
    """读取有限数值，缺失或无效数据保持未知。"""
    value = quality.get(key)

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None

    number = float(value)

    return number if math.isfinite(number) else None


def _known_first(
    value: float | None,
    higher_is_better: bool = False,
) -> tuple[int, float]:
    """已知数据优先，未知数据不伪装成零或最好值。"""
    if value is None:
        return (1, 0.0)

    return (0, -value if higher_is_better else value)


def _candidate_quality_key(
    candidate: dict[str, Any],
) -> tuple[Any, ...]:
    """按计划规定的分层真实质量数据比较，不生成综合评分。"""
    quality = candidate.get("quality")

    if not isinstance(quality, dict):
        quality = {}

    request_count = _quality_number(quality, "request_count")
    success_count = _quality_number(
        quality,
        "request_success_count",
    )
    timeout_count = _quality_number(quality, "timeout_count")

    total_elapsed = _quality_number(
        quality,
        "total_elapsed_seconds",
    )
    total_read = _quality_number(
        quality,
        "total_read_seconds",
    )
    total_bytes = _quality_number(
        quality,
        "total_bytes_read",
    )

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

    if candidate.get("media_type") == "HLS":
        segment_total = _quality_number(
            quality,
            "segment_total",
        )
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

        parts.append(
            _known_first(
                segment_ratio,
                higher_is_better=True,
            )
        )
        parts.append(
            _known_first(
                segment_success,
                higher_is_better=True,
            )
        )

    parts.extend((
        _known_first(timeout_count),
        _known_first(
            success_ratio,
            higher_is_better=True,
        ),
        _known_first(average_elapsed),
        _known_first(average_read),
        _known_first(
            throughput,
            higher_is_better=True,
        ),
    ))

    return tuple(parts)


def _audit_quality_key(
    key: tuple[Any, ...],
) -> list[dict[str, Any]]:
    """转换为可复核记录，明确标记未知数据。"""
    return [
        {
            "known": part[0] == 0,
            "value": part[1] if part[0] == 0 else None,
        }
        for part in key
    ]


def prioritize_candidates_for_globalping(
    candidates: list[dict[str, Any]],
    channels: list[str],
) -> dict[str, Any]:
    """
    Publisher 唯一公开的本地优选入口。

    返回：
      queue：全部本地合格候选组成的完整优选队列；
      excluded：本地不合格候选及其排除原因；
      ranking：候选身份、优选顺序和可复核排序依据。

    不执行 Globalping 远程检测、正式准入、名额竞争、
    订阅生成、安全发布或发布回滚。
    """
    if not isinstance(candidates, list):
        raise PublisherError("Publisher 输入必须是候选列表")

    if (
        not isinstance(channels, list)
        or any(
            not isinstance(channel, str) or not channel
            for channel in channels
        )
        or len(channels) != len(set(channels))
    ):
        raise PublisherError("Publisher 频道顺序无效")

    groups: dict[
        tuple[str, str],
        list[tuple[int, dict[str, Any]]],
    ] = {}

    first_channel_position: dict[str, int] = {}
    first_group_position: dict[tuple[str, str], int] = {}

    excluded: list[dict[str, Any]] = []
    seen_identities: set[tuple[str, str]] = set()

    for original_index, candidate in enumerate(candidates):
        if not isinstance(candidate, dict):
            raise PublisherError(
                f"Publisher 输入第 {original_index + 1} 条不是候选对象"
            )

        channel = candidate.get("channel")
        url = candidate.get("normalized_url")
        media_type = candidate.get("media_type")

        if (
            not isinstance(channel, str)
            or not channel
            or not isinstance(url, str)
            or not url
            or not isinstance(media_type, str)
            or not media_type
        ):
            raise PublisherError(
                f"Publisher 输入第 {original_index + 1} 条"
                "缺少候选身份或媒体类型"
            )

        identity = (channel, url)

        if identity in seen_identities:
            raise PublisherError(
                f"Publisher 输入存在重复候选身份：{channel}"
            )

        seen_identities.add(identity)

        first_channel_position.setdefault(
            channel,
            len(first_channel_position),
        )

        group_key = (channel, media_type)

        first_group_position.setdefault(
            group_key,
            original_index,
        )

        reason = _local_exclusion_reason(candidate)

        if reason is not None:
            excluded.append({
                "candidate": candidate,
                "identity": identity,
                "reason": reason,
            })
            continue

        groups.setdefault(group_key, []).append(
            (original_index, candidate)
        )

    configured_position = {
        channel: index
        for index, channel in enumerate(channels)
    }

    group_keys = sorted(
        groups,
        key=lambda key: (
            configured_position.get(
                key[0],
                len(channels),
            ),
            (
                0
                if key[0] in configured_position
                else first_channel_position[key[0]] + 1
            ),
            first_group_position[key],
        ),
    )

    # 每个频道、媒体类型组独立排序。
    # 排序键相同时，Python 稳定排序保留原始候选顺序。
    queues: list[
        tuple[
            tuple[str, str],
            list[tuple[int, dict[str, Any]]],
        ]
    ] = []

    for group_key in group_keys:
        ranked_group = sorted(
            groups[group_key],
            key=lambda item: _candidate_quality_key(item[1]),
        )

        queues.append((group_key, ranked_group))

    # 各组按轮次交错输出，保留组内质量排序结果。
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
                "identity": (
                    channel,
                    candidate["normalized_url"],
                ),
                "original_index": original_index,
                "priority_order": len(queue),
                "group": {
                    "channel": channel,
                    "media_type": media_type,
                },
                "local_status": local_status,
                "quality_key": _audit_quality_key(
                    quality_key
                ),
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
