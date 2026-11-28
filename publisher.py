"""IPTV-Direct：线路正式准入、频道统计和订阅内容生成。"""

from __future__ import annotations

import math
import os
import tempfile
from pathlib import Path
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
    """线路准入或订阅内容生成错误，并记录正式订阅状态。"""

    def __init__(
        self,
        message: str,
        subscription_state: str = "UNKNOWN",
    ):
        super().__init__(message)
        self.subscription_state = subscription_state


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
    stream_quality = candidate.get("stream_quality")

    if (
        not isinstance(stream_quality, dict)
        or stream_quality.get("status") != PASS
    ):
        return "视频质量检测未明确通过"

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

def _globalping_required_checks(
    media_type: str,
) -> tuple[str, ...] | None:
    """返回 Globalping 按媒体类型必须通过的检测项目。"""
    if media_type == "HLS":
        return ("ipv4", "http", "hls")

    if media_type == "DIRECT_TS":
        return ("ipv4", "http")

    return None



def _globalping_admission_reason(
    candidate: dict[str, Any],
) -> str | None:
    """核验 Globalping 总体、分项及三个不同 ASN 阶段的完整证据。"""
    remote = candidate.get("globalping")

    if not isinstance(remote, dict):
        return "缺少 Globalping 检测结果"

    overall = remote.get("status")

    if overall != PASS:
        return f"Globalping 总体状态不是 PASS：{overall}"

    media_type = candidate.get("media_type")
    required = _globalping_required_checks(media_type)

    if required is None:
        return "未知或不支持的媒体类型，不允许准入"

    checks = remote.get("checks")

    if not isinstance(checks, dict):
        return "缺少 Globalping 分项检测结果"

    if _status(checks, "ts") != NA:
        return "Globalping TS 状态必须为 N/A，由本地检测负责"

    if media_type == "DIRECT_TS":
        if _status(checks, "hls") != NA:
            return "Globalping 直接 TS 的 HLS 状态必须为 N/A"

    for name in required:
        state = _status(checks, name)

        if state != PASS:
            return (
                f"Globalping {name.upper()} 检测未通过："
                f"{state or '缺少结果'}"
            )

    stages = remote.get("stages")

    expected_names = ("probe_1", "probe_2", "probe_3")

    if not isinstance(stages, dict) or set(stages) != set(expected_names):
        return "Globalping 缺少三个完整探针阶段记录"

    used_asns = set()

    for stage_name in expected_names:
        stage = stages.get(stage_name)

        if not isinstance(stage, dict):
            return f"Globalping {stage_name} 阶段记录无效"

        if stage.get("status") != PASS:
            return f"Globalping {stage_name} 阶段没有明确 PASS"

        asn = stage.get("asn")

        if (
            isinstance(asn, bool)
            or not isinstance(asn, int)
            or asn <= 0
            or asn in used_asns
        ):
            return f"Globalping {stage_name} 阶段 ASN 无效或重复"

        used_asns.add(asn)

        probe = stage.get("probe")

        if (
            not isinstance(probe, dict)
            or probe.get("country") != "CN"
            or probe.get("asn") != asn
            or not isinstance(probe.get("network"), str)
            or not probe["network"].strip()
        ):
            return f"Globalping {stage_name} 探针身份信息不完整"

        measurement_id = stage.get("measurement_id")

        if not isinstance(measurement_id, str) or not measurement_id:
            return f"Globalping {stage_name} 缺少测量 ID"

        stage_checks = stage.get("checks")

        if not isinstance(stage_checks, dict):
            return f"Globalping {stage_name} 缺少分项检测结果"

        if _status(stage_checks, "ts") != NA:
            return f"Globalping {stage_name} TS 状态必须为 N/A"

        if media_type == "DIRECT_TS":
            if _status(stage_checks, "hls") != NA:
                return f"Globalping {stage_name} HLS 状态必须为 N/A"

        for name in required:
            if _status(stage_checks, name) != PASS:
                return (
                    f"Globalping {stage_name} 的 {name.upper()} "
                    "检测没有明确 PASS"
                )

    if len(used_asns) != 3:
        return "Globalping 必须使用三个不同 ASN"

    return None


def _quality_order(candidate: dict[str, Any]) -> tuple[float, float]:
    """按分辨率像素总量、实际帧率降序排列，异常值按 0 处理。"""
    quality = candidate.get("stream_quality")

    if not isinstance(quality, dict):
        quality = {}

    def number(name: str) -> float:
        value = quality.get(name)

        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return 0.0

        value = float(value)

        if not math.isfinite(value) or value < 0:
            return 0.0

        return value

    width = number("width")
    height = number("height")
    fps = number("fps")

    return (
        -(width * height),
        -fps,
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

def next_candidate_for_detection(
    candidates: list[dict[str, Any]],
    accepted_routes: list[dict[str, Any]],
    channels: list[str],
    settings: dict[str, Any],
) -> dict[str, Any] | None:
    """按质量顺序选择下一条候选，但不因现有线路名额跳过检测。"""
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

    if selection.get("max_routes_per_channel") != MAX_ROUTES_PER_CHANNEL:
        raise PublisherError(
            "每个频道最多保留线路数必须符合 MASTER_PLAN.md：2"
        )

    # 本批需要重新检测的历史线路，不得沿用旧结果占位。
    pending_urls = {
        item.get("normalized_url")
        for item in candidates
        if isinstance(item, dict)
        and isinstance(item.get("normalized_url"), str)
    }

    seen_urls = set()

    for route in accepted_routes:
        if not isinstance(route, dict):
            raise PublisherError("累计准入数据中存在无效线路")

        prepared, _ = _prepare_candidate(route, channels)

        if prepared is None:
            continue

        url = prepared["normalized_url"]

        if url not in pending_urls:
            seen_urls.add(url)

    # 只按质量排序；质量完全相同时使用 URL 稳定排序。
    indexed = [
        (index, candidate)
        for index, candidate in enumerate(candidates)
        if isinstance(candidate, dict)
    ]

    indexed.sort(
        key=lambda item: (
            *_quality_order(item[1]),
            (
                item[1].get("normalized_url")
                if isinstance(item[1].get("normalized_url"), str)
                else ""
            ),
        )
    )

    for _, candidate in indexed:
        channel = candidate.get("channel")

        if channel not in channels:
            continue

        url = candidate.get("normalized_url")

        if not _valid_url(url) or url in seen_urls:
            continue

        if _local_admission_reason(candidate):
            continue

        # 不检查当前频道已有几条线路。
        # 每条符合前置条件的候选都必须完成后续检测。
        return candidate

    return None      

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

    # 历史线路必须具备完整检测证据，且与本批候选共同参与质量竞争。
    existing = []
    historical_urls = set()
    current_urls = {
        item.get("normalized_url")
        for item in candidates
        if isinstance(item, dict)
        and isinstance(item.get("normalized_url"), str)
    }

    for index, route in enumerate(accepted_routes):
        if not isinstance(route, dict):
            raise PublisherError(
                f"历史第 {index + 1} 条线路不是有效对象"
            )

        channel = route.get("channel")

        if not isinstance(channel, str) or channel not in channels:
            raise PublisherError(
                f"历史第 {index + 1} 条线路频道无效"
            )

        url = route.get("normalized_url")

        if not _valid_url(url):
            raise PublisherError(
                f"历史第 {index + 1} 条线路 URL 无效"
            )

        if url in historical_urls:
            raise PublisherError(
                f"历史第 {index + 1} 条线路 URL 重复"
            )

        historical_urls.add(url)

        # 本批重新检测的线路以本批结果为准，不沿用旧检测结果。
        if url in current_urls:
            continue

        prepared_route, _ = _prepare_candidate(route, channels)

        # 历史线路缺少完整准入证据时，不得占据正式名额。
        if prepared_route is None:
            continue

        existing.append(prepared_route)

    prepared_by_index = {}
    rejected_by_index = {}
    pool = []

    for index, candidate in enumerate(candidates):
        route, reason = _prepare_candidate(candidate, channels)

        if route is None:
            rejected_by_index[index] = {
                "candidate": candidate,
                "status": REJECTED,
                "reason": reason,
            }
            continue

        prepared_by_index[index] = route
        pool.append((route, "current", index))

    for index, route in enumerate(existing):
        pool.append((route, "history", index))

    # 历史线路与本批线路使用完全相同的质量标准。
    # 质量相同时只使用规范化 URL 保证排序稳定，
    # 不得使用来源优先级、来源顺序或历史身份决定去留。
    pool.sort(
        key=lambda item: (
            *_quality_order(item[0]),
            item[0]["normalized_url"],
        )
    )

    retained = []
    counts = {channel: 0 for channel in channels}
    seen_urls = set()
    accepted_current = set()
    rejected_reasons = {}

    for route, origin, index in pool:
        url = route["normalized_url"]
        channel = route["channel"]

        if url in seen_urls:
            if origin == "current":
                rejected_reasons[index] = "线路 URL 已存在，不重复准入"
            continue

        if counts[channel] >= MAX_ROUTES_PER_CHANNEL:
            if origin == "current":
                rejected_reasons[index] = (
                    "该频道质量排名未进入最终两条线路"
                )
            continue

        seen_urls.add(url)
        retained.append(route)
        counts[channel] += 1

        if origin == "current":
            accepted_current.add(index)

    results = []

    for index, candidate in enumerate(candidates):
        if index in rejected_by_index:
            results.append(rejected_by_index[index])
        elif index in accepted_current:
            route = prepared_by_index[index]
            results.append({
                "candidate": route,
                "status": ACCEPTED,
                "reason": route["selection"]["reason"],
            })
        else:
            results.append({
                "candidate": candidate,
                "status": REJECTED,
                "reason": rejected_reasons.get(
                    index,
                    "该频道质量排名未进入最终两条线路",
                ),
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
            *_quality_order(route),
            route["normalized_url"],
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
            ("channel", "tvg-name"),
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
    event_callback=None,
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

    event_callback 仅用于报告发布事件，不参与发布判断。
    """

    def emit(stage, status, detail):
        if event_callback is None:
            return

        try:
            event_callback(stage, status, detail)
        except Exception:
            pass

    emit(
        "发布检查",
        "STARTED",
        "开始检查新订阅内容和发布条件",
    )

    if not isinstance(subscriptions, dict):
        raise PublisherError(
            "订阅数据格式错误",
            subscription_state="UNMODIFIED",
        )

    m3u_text = subscriptions.get("m3u")
    txt_text = subscriptions.get("txt")

    try:
        validate_subscriptions(
            m3u_text,
            txt_text,
            channels,
        )

        new_snapshot = _subscription_snapshot(
            m3u_text,
            txt_text,
        )
    except PublisherError as exc:
        exc.subscription_state = "UNMODIFIED"
        raise

    if subscriptions.get("route_count") != new_snapshot["route_count"]:
        raise PublisherError(
            "订阅线路数量统计不一致",
            subscription_state="UNMODIFIED",
        )

    directory = Path(output_dir)

    targets = [
        directory / "iptv.m3u",
        directory / "iptv.txt",
    ]

    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PublisherError(
            f"发布前创建输出目录失败：{exc}",
            subscription_state="UNMODIFIED",
        ) from exc
    old_data = []

    for target in targets:
        try:
            data = target.read_bytes()
        except FileNotFoundError:
            data = None
        except OSError as exc:
            raise PublisherError(
                f"发布前读取旧订阅文件失败：{target.name}：{exc}",
                subscription_state="UNMODIFIED",
            ) from exc

        old_data.append(data)

    has_m3u = old_data[0] is not None
    has_txt = old_data[1] is not None

    if has_m3u != has_txt:
        raise PublisherError(
            "旧订阅只有一份存在有效内容，阻止发布",
            subscription_state="UNMODIFIED",
        )

    first_publish = not has_m3u and not has_txt

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
                f"旧订阅损坏，阻止覆盖：{exc}",
                subscription_state="UNMODIFIED",
            ) from exc

        old_channel_count = old_snapshot["channel_count"]
        new_channel_count = new_snapshot["channel_count"]
        old_route_count = old_snapshot["route_count"]
        new_route_count = new_snapshot["route_count"]

        channel_drop = (
            (old_channel_count - new_channel_count)
            / old_channel_count * 100
            if old_channel_count
            else 0
        )

        route_drop = (
            (old_route_count - new_route_count)
            / old_route_count * 100
            if old_route_count
            else 0
        )

        if (
            new_channel_count * 2
            < old_channel_count
        ):
            emit(
                "发布保护",
                "FAIL",
                (
                    f"频道数量下降超过50%；"
                    f"旧频道 {old_channel_count} 个；"
                    f"新频道 {new_channel_count} 个；"
                    f"下降 {channel_drop:.1f}%"
                ),
            )
            raise PublisherError(
                f"频道数量下降超过50%，阻止发布；"
                f"旧频道 {old_channel_count} 个；"
                f"新频道 {new_channel_count} 个；"
                f"下降 {channel_drop:.1f}%；"
                f"旧线路 {old_route_count} 条；"
                f"新线路 {new_route_count} 条。",
                subscription_state="UNMODIFIED",
            )

        if (
            new_route_count * 2
            < old_route_count
        ):
            emit(
                "发布保护",
                "FAIL",
                (
                    f"线路数量下降超过50%；"
                    f"旧线路 {old_route_count} 条；"
                    f"新线路 {new_route_count} 条；"
                    f"下降 {route_drop:.1f}%"
                ),
            )
            raise PublisherError(
                f"线路数量下降超过50%，阻止发布；"
                f"旧线路 {old_route_count} 条；"
                f"新线路 {new_route_count} 条；"
                f"下降 {route_drop:.1f}%；"
                f"旧频道 {old_channel_count} 个；"
                f"新频道 {new_channel_count} 个。",
                subscription_state="UNMODIFIED",
            )

    emit(
        "旧订阅检查",
        "COMPLETED",
        (
            "首次发布，没有旧订阅文件"
            if first_publish
            else "旧 M3U/TXT 文件均存在，检查通过"
        ),
    )

    new_data = [
        m3u_text.encode("utf-8"),
        txt_text.encode("utf-8"),
    ]

    staged = []

    try:
        emit(
            "临时文件准备",
            "STARTED",
            "开始准备两份正式订阅临时文件",
        )

        # 先准备好两份临时文件。
        try:
            for target, data in zip(targets, new_data):
                staged.append(
                    _write_temp_file(
                        directory,
                        f".{target.name}.stage-",
                        data,
                    )
                )
        except Exception as exc:
            emit(
                "临时文件准备",
                "FAIL",
                f"临时文件准备失败，正式订阅未修改：{exc}",
            )
            raise PublisherError(
                f"临时文件准备失败，正式订阅未修改：{exc}",
                subscription_state="UNMODIFIED",
            ) from exc

        emit(
            "临时文件准备",
            "COMPLETED",
            "两份临时文件均已写入并完成落盘",
        )


        publish_stage = "正式文件替换"

        try:
            # 依次替换正式文件。
            emit(
                "正式文件替换",
                "STARTED",
                "开始替换正式 M3U/TXT 文件",
            )

            for temp_file, target in zip(staged, targets):
                os.replace(temp_file, target)
                emit(
                    "正式文件替换",
                    "PROGRESS",
                    f"已替换 {target.name}",
                )

            emit(
                "正式文件替换",
                "COMPLETED",
                "两份正式文件均已完成替换",
            )

            # 重新读取正式文件。
            publish_stage = "发布后校验"
            emit(
                "发布后校验",
                "STARTED",
                "开始重新读取正式 M3U/TXT 文件",
            )

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

            emit(
                "发布后校验",
                "COMPLETED",
                "正式 M3U/TXT 内容及线路数量核验通过",
            )

        except Exception as publish_error:
            emit(
                publish_stage,
                "FAIL",
                f"{publish_stage}失败：{publish_error}",
            )

            emit(
                "正式订阅回滚",
                "STARTED",
                "发布过程发生异常，开始恢复旧文件",
            )

            rollback_errors = _restore_subscription_files(
                targets,
                old_data,
                directory,
            )

            if rollback_errors:
                emit(
                    "正式订阅回滚",
                    "FAIL",
                    "；".join(rollback_errors),
                )
                raise PublisherError(
                    f"发布失败：{publish_error}；"
                    f"回滚失败：{'；'.join(rollback_errors)}",
                    subscription_state="ROLLBACK_FAILED",
                ) from publish_error

            emit(
                "正式订阅回滚",
                "COMPLETED",
                "旧订阅文件已恢复",
            )

            raise PublisherError(
                f"发布失败，旧文件已恢复：{publish_error}",
                subscription_state="RESTORED",
            ) from publish_error


        except Exception as publish_error:
            emit(
                "正式订阅回滚",
                "STARTED",
                "发布过程发生异常，开始恢复旧文件",
            )

            rollback_errors = _restore_subscription_files(
                targets,
                old_data,
                directory,
            )

            if rollback_errors:
                emit(
                    "正式订阅回滚",
                    "FAIL",
                    "；".join(rollback_errors),
                )
                raise PublisherError(
                    f"发布失败：{publish_error}；"
                    f"回滚失败：{'；'.join(rollback_errors)}",
                    subscription_state="ROLLBACK_FAILED",
                ) from publish_error

            emit(
                "正式订阅回滚",
                "COMPLETED",
                "旧订阅文件已恢复",
            )

            raise PublisherError(
                f"发布失败，旧文件已恢复：{publish_error}",
                subscription_state="RESTORED",
            ) from publish_error

    finally:
        cleanup_errors = []

        for temp_file in staged:
            try:
                temp_file.unlink(missing_ok=True)
            except OSError as exc:
                cleanup_errors.append(
                    f"{temp_file.name} 清理失败：{exc}"
                )

        if cleanup_errors:
            emit(
                "临时文件清理",
                "WARNING",
                "；".join(cleanup_errors),
            )

    emit(
        "发布流程",
        "COMPLETED",
        (
            f"频道 {new_snapshot['channel_count']} 个；"
            f"线路 {new_snapshot['route_count']} 条"
        ),
    )

    return {
        "published": True,
        "channel_count": new_snapshot["channel_count"],
        "route_count": new_snapshot["route_count"],
        "m3u_path": str(targets[0]),
        "txt_path": str(targets[1]),
        "first_publish": first_publish,
    }


