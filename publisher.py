
"""IPTV-Direct：线路正式准入、频道统计和订阅内容生成。"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from normalizer import normalize_url


PASS = "PASS"
FAIL = "FAIL"
INCOMPLETE = "INCOMPLETE"
NA = "N/A"

ACCEPTED = "ACCEPTED"
REJECTED = "REJECTED"

MAX_ROUTES_PER_CHANNEL = 2


class PublisherError(ValueError):
    """线路准入或订阅内容生成错误。"""


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
    """确认候选 URL 是规范化后的有效 HTTP/HTTPS 地址。"""
    if not isinstance(value, str) or not value:
        return False

    try:
        parsed = urlsplit(value)

        if (
            parsed.scheme.lower() not in ("http", "https")
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            return False

        return normalize_url(value) == value

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
    """读取来源优先级和来源内原始顺序。"""
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

    # 国内真实网络验证与本地、Globalping 检测分别记录。
    # 没有实测证据时绝不标记为国内实播通过。
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
      channel_counts：各频道累计有效线路数量。
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

    # 先保留此前批次已经准入的线路。
    existing = []

    for route in accepted_routes:
        if not isinstance(route, dict):
            raise PublisherError("累计准入数据中存在无效线路")

        existing.append(dict(route))

    seen_urls = {
        route.get("normalized_url")
        for route in existing
        if isinstance(route.get("normalized_url"), str)
    }

    prepared = []
    results = []

    # 本批按人工优先级、来源原始顺序处理。
    indexed = list(enumerate(candidates))

    def sort_key(item):
        index, candidate = item

        if not isinstance(candidate, dict):
            return (999999, 999999, index)

        try:
            priority, order = _candidate_order(candidate)
            return (priority, order, index)
        except PublisherError:
            return (999999, 999999, index)

    indexed.sort(key=sort_key)

    for _, candidate in indexed:
        route, reason = _prepare_candidate(candidate, channels)

        if route is None:
            results.append({
                "candidate": candidate,
                "status": REJECTED,
                "reason": reason,
            })
            continue

        url = route["normalized_url"]

        if url in seen_urls:
            results.append({
                "candidate": candidate,
                "status": REJECTED,
                "reason": "线路 URL 已存在，不重复准入",
            })
            continue

        seen_urls.add(url)
        prepared.append(route)

    # 合并历史累计线路和本批候选，再按正式优先级整理。
    combined = existing + prepared

    channel_index = {
        channel: index
        for index, channel in enumerate(channels)
    }

    combined.sort(
        key=lambda route: (
            channel_index.get(route.get("channel"), len(channels)),
            *_candidate_order(route),
        )
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

    retained_new_urls = {
        route["normalized_url"]
        for route in retained
        if route["normalized_url"] not in {
            old.get("normalized_url") for old in existing
        }
    }

    # 为本批每条候选生成明确结果。
    for item in prepared:
        url = item["normalized_url"]

        if url in retained_new_urls:
            results.append({
                "candidate": item,
                "status": ACCEPTED,
                "reason": item["selection"]["reason"],
            })
        else:
            results.append({
                "candidate": item,
                "status": REJECTED,
                "reason": "该频道已达到最多保留 2 条线路的上限",
            })

    all_reached = bool(channels) and all(
        counts[channel] >= MAX_ROUTES_PER_CHANNEL
        for channel in channels
    )

    return {
        "results": results,
        "accepted_routes": retained,
        "channel_counts": counts,
        "all_channels_reached": all_reached,
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
    return (
        isinstance(route, dict)
        and route.get("channel") in channels
        and _valid_url(route.get("normalized_url"))
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
        key=lambda route: (
            channel_index[route["channel"]],
            *_candidate_order(route),
        )
    )

    counts = _channel_counts(ordered, channels)

    if any(value > MAX_ROUTES_PER_CHANNEL for value in counts.values()):
        raise PublisherError("最终线路数量超过每频道 2 条上限")

    urls = [route["normalized_url"] for route in ordered]

    if len(urls) != len(set(urls)):
        raise PublisherError("最终订阅存在重复 URL")

    m3u_lines = ["#EXTM3U"]
    txt_lines = []

    for route in ordered:
        channel = route["channel"]
        url = route["normalized_url"]

        if any(char in channel for char in ("\r", "\n", ",")):
            raise PublisherError("频道名称包含不支持的格式字符")

        attributes = []

        for key, attribute in (
            ("tvg_id", "tvg-id"),
            ("tvg_name", "tvg-name"),
            ("category", "group-title"),
        ):
            value = _safe_m3u_attribute(route.get(key))

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

        if not _valid_url(url):
            raise PublisherError("M3U 包含无效播放 URL")

        m3u_entries.append((channel, url))
        index += 2

    # 没有任何有效频道线路时，不能把空订阅视为校验通过。
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
