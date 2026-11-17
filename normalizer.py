
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""IPTV-Direct：来源解析、频道匹配与 URL 归一。"""

import re
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit


class ChannelConfigError(ValueError):
    """目标频道配置错误。"""


class ChannelList(list):
    """兼容列表接口，同时保存频道分类及分类顺序。"""

    def __init__(self, channels, category_by_channel, category_order):
        super().__init__(channels)
        self.category_by_channel = dict(category_by_channel)
        self.category_order = tuple(category_order)


def load_channels(path):
    """从 channels.txt 动态读取分类、顺序及目标频道。"""
    try:
        lines = Path(path).read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        raise ChannelConfigError(f"读取 channels.txt 失败：{exc}") from exc

    channels = []
    category_by_channel = {}
    category_order = []
    category_keys = set()
    current_category = None

    for raw_line in lines:
        line = raw_line.strip()

        if not line:
            continue

        if line.startswith("#"):
            heading = line.lstrip("#").strip()

            if not heading:
                raise ChannelConfigError("channels.txt 存在空分类标题")

            category_key = heading.casefold()

            if category_key in category_keys:
                raise ChannelConfigError(
                    f"channels.txt 分类标题重复：{heading}"
                )

            category_keys.add(category_key)
            category_order.append(heading)
            current_category = heading
            continue

        if current_category is None:
            raise ChannelConfigError(
                f"频道必须放在分类标题下面：{line}"
            )

        if line in category_by_channel:
            previous = category_by_channel[line]
            raise ChannelConfigError(
                f"频道重复配置：{line}（{previous}、{current_category}）"
            )

        channels.append(line)
        category_by_channel[line] = current_category

    if not category_order:
        raise ChannelConfigError("channels.txt 没有配置分类标题")

    if not channels:
        raise ChannelConfigError("channels.txt 没有配置目标频道")

    return ChannelList(
        channels,
        category_by_channel,
        category_order,
    )


def _channel_category(channels, channel):
    """只读取目标频道配置中的分类，不采信直播源自带分类。"""
    if not channel:
        return ""

    category_by_channel = getattr(
        channels,
        "category_by_channel",
        {},
    )

    return category_by_channel.get(channel, "")


def _is_absolute_http_url(value):
    """判断是否为绝对 HTTP/HTTPS URL，不区分协议头大小写。"""
    if not isinstance(value, str) or not value:
        return False

    try:
        parsed = urlsplit(value)
        return (
            parsed.scheme.lower() in ("http", "https")
            and bool(parsed.netloc)
            and bool(parsed.hostname)
        )
    except ValueError:
        return False


def normalize_url(value):
    """规范化 HTTP/HTTPS URL，保留有效端口和查询参数。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("URL 为空")

    value = value.strip()

    if any(char.isspace() for char in value):
        raise ValueError("URL 包含空白字符")

    try:
        parsed = urlsplit(value)
        scheme = parsed.scheme.lower()
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError("URL 格式无效") from exc

    if (
        scheme not in ("http", "https")
        or not host
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("只接受不含用户名密码的 HTTP/HTTPS URL")

    if parsed.netloc.endswith(":"):
        raise ValueError("URL 端口格式无效")

    if port is not None and not 1 <= port <= 65535:
        raise ValueError("URL 端口无效")

    host = host.lower()

    if ":" in host:
        host = f"[{host}]"

    netloc = host

    if port is not None and not (
        (scheme == "http" and port == 80)
        or (scheme == "https" and port == 443)
    ):
        netloc += f":{port}"

    return urlunsplit((
        scheme,
        netloc,
        parsed.path or "/",
        parsed.query,
        "",
    ))


def _channel_match(names, channels, aliases):
    """只按标准名称或明确别名精确匹配。"""
    targets = {name.casefold(): name for name in channels}

    if aliases is not None and not isinstance(aliases, dict):
        raise TypeError("aliases 必须是字典")

    alias_map = {
        str(alias).casefold(): canonical
        for alias, canonical in (aliases or {}).items()
    }

    for name in names:
        name = (name or "").strip()

        if not name:
            continue

        canonical = alias_map.get(name.casefold(), name)
        matched = targets.get(str(canonical).casefold())

        if matched:
            return matched

    return None


def _parse_attributes(text):
    return dict(re.findall(r'([\w-]+)="([^"]*)"', text))


def _looks_like_ts(data):
    """检查连续五个 188 字节 TS 同步位置。"""
    if len(data) < 188 * 4 + 1:
        return False

    return any(
        all(data[offset + 188 * index] == 0x47 for index in range(5))
        for offset in range(min(188, len(data) - 188 * 4))
    )


def parse_source_content(
    content,
    source,
    channels,
    content_type="",
    max_hls_manifest_bytes=1048576,
    aliases=None,
    max_candidates=None,
):
    """识别 TXT、M3U、HLS 和媒体内容，按上限提取候选。"""
    if isinstance(content, bytes):
        raw = content
        text = content.decode("utf-8-sig", errors="replace")
    elif isinstance(content, str):
        text = content.lstrip("\ufeff")
        raw = text.encode("utf-8")
    else:
        raise TypeError("来源内容必须是 bytes 或 str")

    if max_candidates is not None and (
        not isinstance(max_candidates, int)
        or isinstance(max_candidates, bool)
        or max_candidates < 1
    ):
        raise ValueError("解析候选上限必须是正整数")

    lines = [
        line.strip()
        for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    ]
    nonempty = [line for line in lines if line]

    if not nonempty:
        return {
            "recognized": False,
            "format": "unknown",
            "candidates": [],
            "errors": ["来源内容为空"],
        }

    base = source.get("final_url") or source.get("url", "")
    media_type = (content_type or "").lower()

    is_hls = any(
        line.startswith("#EXT-X-")
        for line in nonempty[:30]
    )
    is_ts = _looks_like_ts(raw)

    is_media_type = (
        media_type.startswith(("video/", "audio/"))
        and "mpegurl" not in media_type
    )

    if is_hls or is_ts or is_media_type:
        fmt = "hls" if is_hls else "media"

        if is_hls and len(raw) > max_hls_manifest_bytes:
            return {
                "recognized": False,
                "format": fmt,
                "candidates": [],
                "errors": ["HLS 媒体清单超过读取上限"],
            }

        return {
            "recognized": True,
            "format": fmt,
            "candidates": [{
                "channel": None,
                "raw_url": base,
                "tvg_id": "",
                "tvg_name": "",
                "category": "",
                "source_url": source.get("url", ""),
                "source_type": source.get("source_type", ""),
                "priority": source.get("priority", 1),
                "source_order": source.get("order", 0),
            }],
            "errors": ["媒体来源未提供可确认的频道名称"],
        }

    is_m3u = any(
        line.upper().startswith("#EXTM3U")
        for line in nonempty[:3]
    )
    fmt = "m3u" if is_m3u else "txt"

    candidates = []
    errors = []
    pending = None

    target_candidate_count = 0

    def add_candidate(candidate, error=None):
        nonlocal target_candidate_count

        candidates.append(candidate)

        if error:
            errors.append(error)

        if candidate.get("channel") is not None:
            target_candidate_count += 1

        # 保留原有解析函数的独立上限接口。
        # 正式归一流程会传入 None，在 URL 验证和去重后计算额度。
        return (
            max_candidates is not None
            and target_candidate_count > max_candidates
        )

    for line in lines:
        if not line:
            continue

        if line.upper().startswith("#EXTINF:"):
            title = line.split(",", 1)[1].strip() if "," in line else ""
            pending = (title, _parse_attributes(line))
            continue

        if line.startswith("#"):
            continue

        is_absolute_url = _is_absolute_http_url(line)
        is_m3u_url = is_m3u and not line.startswith("#")

        if is_absolute_url or is_m3u_url:
            if is_m3u:
                title, attrs = pending or ("", {})
                names = [
                    attrs.get("tvg-name", ""),
                    attrs.get("tvg-id", ""),
                    title,
                ]
                raw_url = urljoin(base, line)
            else:
                names = []
                attrs = {}
                raw_url = line

            channel = _channel_match(names, channels, aliases)

            candidate = {
                "channel": channel,
                "raw_url": raw_url,
                "tvg_id": attrs.get("tvg-id", "") if is_m3u else "",
                "tvg_name": attrs.get("tvg-name", "") if is_m3u else "",
                "category": _channel_category(channels, channel),
                "source_url": source.get("url", ""),
                "source_type": source.get("source_type", ""),
                "priority": source.get("priority", 1),
                "source_order": source.get("order", 0),
            }

            pending = None

            error = None if channel else (
                f"线路缺少可确认的目标频道信息：{line[:100]}"
            )

            if add_candidate(candidate, error):
                break

            continue

        if not is_m3u and "," in line:
            name, url = (part.strip() for part in line.split(",", 1))

            if _is_absolute_http_url(url):
                channel = _channel_match([name], channels, aliases)

                candidate = {
                    "channel": channel,
                    "raw_url": url,
                    "tvg_id": "",
                    "tvg_name": name,
                    "category": _channel_category(channels, channel),
                    "source_url": source.get("url", ""),
                    "source_type": source.get("source_type", ""),
                    "priority": source.get("priority", 1),
                    "source_order": source.get("order", 0),
                }

                error = None if channel else (
                    f"频道名称不在目标频道配置中：{name}"
                )

                if add_candidate(candidate, error):
                    break

    if not candidates:
        errors.append("未提取到直播线路")

    return {
        "recognized": bool(candidates),
        "format": fmt,
        "candidates": candidates,
        "errors": errors,
    }


def normalize_source_content(
    content,
    source,
    channels,
    max_candidates_per_source,
    seen_urls,
    content_type="",
    max_hls_manifest_bytes=1048576,
    aliases=None,
    max_candidates_remaining=None,
):
    """只让有效且全局唯一的目标频道 URL 占用候选额度。"""
    for value, name in (
        (max_candidates_per_source, "单来源候选上限"),
        (max_candidates_remaining, "本轮剩余候选额度"),
    ):
        if value is not None and (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < 1
        ):
            raise ValueError(f"{name}必须是正整数")

    if not isinstance(seen_urls, set):
        raise TypeError("seen_urls 必须是整轮共用的 set")

    parsed = parse_source_content(
        content,
        source,
        channels,
        content_type,
        max_hls_manifest_bytes,
        aliases,
        # 不在 URL 验证、去重之前截断候选。
        max_candidates=None,
    )

    retained = []
    duplicates = 0
    matched = 0
    invalid = 0
    source_limited = False
    total_limited = False

    if parsed["recognized"]:
        for candidate in parsed["candidates"]:
            # 非目标频道不占额度，也不进入全局去重集合。
            if candidate.get("channel") is None:
                continue

            try:
                url = normalize_url(candidate.get("raw_url"))
            except (TypeError, ValueError):
                invalid += 1
                parsed["errors"].append(
                    f"无效线路 URL："
                    f"{str(candidate.get('raw_url', ''))[:100]}"
                )
                continue

            matched += 1

            # 已经被本轮其他来源收录的线路不占本来源额度。
            if url in seen_urls:
                duplicates += 1
                continue

            # 只有遇到下一条有效且全局唯一的线路，
            # 才能确认额度真正超限。
            source_overflow = (
                len(retained) >= max_candidates_per_source
            )
            total_overflow = (
                max_candidates_remaining is not None
                and len(retained) >= max_candidates_remaining
            )

            if source_overflow or total_overflow:
                source_limited = source_overflow
                total_limited = total_overflow
                break

            # 只有正式收录的有效唯一线路才加入 seen_urls。
            seen_urls.add(url)

            retained.append({
                **candidate,
                "normalized_url": url,
            })

    if source_limited:
        parsed["errors"].append(
            f"达到单来源候选上限 {max_candidates_per_source}，"
            "其余有效唯一候选不再处理"
        )

    if total_limited:
        parsed["errors"].append(
            f"达到本轮剩余候选额度 {max_candidates_remaining}，"
            "其余有效唯一候选不再处理"
        )

    retained_count = len(retained)

    return {
        **parsed,
        "candidates": retained,
        # 全局额度必须累计实际保留的有效唯一线路。
        "extracted_candidates": retained_count,
        "matched_target_candidates": matched,
        "invalid_candidates": invalid,
        "duplicate_candidates": duplicates,
        "retained_candidates": retained_count,
        "limit_reached": source_limited,
        "total_limit_reached": total_limited,
    }
