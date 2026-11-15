
"""IPTV-Direct：来源内容解析、频道匹配与线路归一。"""

import re
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit


class ChannelConfigError(ValueError):
    """目标频道配置错误。"""


def load_channels(path):
    """读取 channels.txt，每行一个频道名称。"""
    try:
        lines = Path(path).read_text(
            encoding="utf-8-sig"
        ).splitlines()
    except OSError as exc:
        raise ChannelConfigError(
            f"读取 channels.txt 失败：{exc}"
        ) from exc

    channels = list(dict.fromkeys(
        line.strip()
        for line in lines
        if line.strip()
        and not line.lstrip().startswith("#")
    ))

    if not channels:
        raise ChannelConfigError(
            "channels.txt 没有配置目标频道"
        )

    return channels


def normalize_url(value):
    """规范化 HTTP/HTTPS URL，保留路径和查询参数。"""
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
        or parsed.username
        or parsed.password
    ):
        raise ValueError(
            "只接受不含用户名密码的 HTTP/HTTPS URL"
        )

    host = host.lower()

    if ":" in host:
        host = f"[{host}]"

    netloc = host

    if port and not (
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
    """只按频道标准名称或明确别名精确匹配。"""
    targets = {
        name.casefold(): name
        for name in channels
    }

    aliases = aliases or {}

    for name in names:
        name = (name or "").strip()

        if not name:
            continue

        canonical = aliases.get(
            name,
            aliases.get(name.casefold(), name),
        )

        matched = targets.get(
            str(canonical).casefold()
        )

        if matched:
            return matched

    return None


def _parse_attributes(text):
    return dict(
        re.findall(r'([\w-]+)="([^"]*)"', text)
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
    """解析来源内容，达到候选上限时提前停止。"""
    errors = []

    if isinstance(content, bytes):
        raw = content
        text = content.decode(
            "utf-8-sig",
            errors="replace",
        )
    elif isinstance(content, str):
        text = content.lstrip("\ufeff")
        raw = text.encode("utf-8")
    else:
        raise TypeError(
            "来源内容必须是 bytes 或 str"
        )

    if max_candidates is not None and (
        not isinstance(max_candidates, int)
        or isinstance(max_candidates, bool)
        or max_candidates < 1
    ):
        raise ValueError(
            "解析候选上限必须是正整数"
        )

    text = text.replace(
        "\r\n", "\n"
    ).replace("\r", "\n")

    lines = [
        line.strip()
        for line in text.split("\n")
    ]

    nonempty = [
        line for line in lines if line
    ]

    if not nonempty:
        return {
            "recognized": False,
            "format": "unknown",
            "candidates": [],
            "errors": ["来源内容为空"],
        }

    base = (
        source.get("final_url")
        or source.get("url", "")
    )

    media_type = (content_type or "").lower()

    is_hls = any(
        line.startswith("#EXT-X-")
        for line in nonempty[:30]
    )

    is_ts = (
        len(raw) >= 188 * 4 + 1
        and any(
            all(
                raw[offset + 188 * index] == 0x47
                for index in range(5)
            )
            for offset in range(
                min(188, len(raw) - 188 * 4)
            )
        )
    )

    if (
        is_hls
        or is_ts
        or media_type.startswith(
            ("video/", "audio/")
        )
    ):
        fmt = (
            "hls"
            if is_hls or "mpegurl" in media_type
            else "media"
        )

        if (
            is_hls
            and len(raw) > max_hls_manifest_bytes
        ):
            return {
                "recognized": False,
                "format": fmt,
                "candidates": [],
                "errors": [
                    "HLS 媒体清单超过读取上限"
                ],
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
                "source_type": source.get(
                    "source_type", ""
                ),
                "priority": source.get(
                    "priority", 1
                ),
                "source_order": source.get(
                    "order", 0
                ),
            }],
            "errors": [
                "媒体来源未提供可确认的频道名称"
            ],
        }

    is_m3u = any(
        line.upper().startswith("#EXTM3U")
        for line in nonempty[:3]
    )

    fmt = "m3u" if is_m3u else "txt"

    candidates = []
    pending = None

    def add_candidate(candidate, error=None):
        candidates.append(candidate)

        if error:
            errors.append(error)

        # 多识别一条，仅用于确认是否超过上限。
        return (
            max_candidates is not None
            and len(candidates) > max_candidates
        )

    for line in lines:
        if not line:
            continue

        if line.upper().startswith("#EXTINF:"):
            title = (
                line.split(",", 1)[1].strip()
                if "," in line
                else ""
            )

            pending = (
                title,
                _parse_attributes(line),
            )
            continue

        if line.startswith("#"):
            continue

        if line.startswith((
            "http://",
            "https://",
            "/",
            "./",
            "../",
        )):
            title, attrs = (
                pending
                if pending
                else ("", {})
            )

            channel = _channel_match(
                [
                    attrs.get("tvg-name", ""),
                    attrs.get("tvg-id", ""),
                    title,
                ],
                channels,
                aliases,
            )

            candidate = {
                "channel": channel,
                "raw_url": urljoin(base, line),
                "tvg_id": attrs.get("tvg-id", ""),
                "tvg_name": attrs.get("tvg-name", ""),
                "category": attrs.get("group-title", ""),
                "source_url": source.get("url", ""),
                "source_type": source.get(
                    "source_type", ""
                ),
                "priority": source.get(
                    "priority", 1
                ),
                "source_order": source.get(
                    "order", 0
                ),
            }

            error = (
                None
                if channel
                else (
                    "线路缺少可确认的目标频道信息："
                    f"{line[:100]}"
                )
            )

            pending = None

            if add_candidate(candidate, error):
                break

            continue

        if not is_m3u and "," in line:
            name, url = (
                part.strip()
                for part in line.split(",", 1)
            )

            if url.startswith((
                "http://",
                "https://",
            )):
                channel = _channel_match(
                    [name],
                    channels,
                    aliases,
                )

                candidate = {
                    "channel": channel,
                    "raw_url": url,
                    "tvg_id": "",
                    "tvg_name": name,
                    "category": "",
                    "source_url": source.get("url", ""),
                    "source_type": source.get(
                        "source_type", ""
                    ),
                    "priority": source.get(
                        "priority", 1
                    ),
                    "source_order": source.get(
                        "order", 0
                    ),
                }

                error = (
                    None
                    if channel
                    else (
                        "频道名称不在目标频道配置中："
                        f"{name}"
                    )
                )

                if add_candidate(candidate, error):
                    break

        elif (
            not is_m3u
            and line.startswith((
                "http://",
                "https://",
            ))
        ):
            candidate = {
                "channel": None,
                "raw_url": line,
                "tvg_id": "",
                "tvg_name": "",
                "category": "",
                "source_url": source.get("url", ""),
                "source_type": source.get(
                    "source_type", ""
                ),
                "priority": source.get(
                    "priority", 1
                ),
                "source_order": source.get(
                    "order", 0
                ),
            }

            if add_candidate(
                candidate,
                (
                    "单条线路没有频道信息，"
                    "无法匹配目标频道："
                    f"{line[:100]}"
                ),
            ):
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
    """解析来源，执行候选上限、URL 归一和整轮去重。"""
    for value, name in (
        (
            max_candidates_per_source,
            "单来源候选上限",
        ),
        (
            max_candidates_remaining,
            "本轮剩余候选额度",
        ),
    ):
        if value is not None and (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < 1
        ):
            raise ValueError(
                f"{name}必须是正整数"
            )

    if not isinstance(seen_urls, set):
        raise TypeError(
            "seen_urls 必须是整轮共用的 set"
        )

    limit = max_candidates_per_source

    if max_candidates_remaining is not None:
        limit = min(
            limit,
            max_candidates_remaining,
        )

    parsed = parse_source_content(
        content,
        source,
        channels,
        content_type,
        max_hls_manifest_bytes,
        aliases,
        limit,
    )

    if not parsed["recognized"]:
        return {
            **parsed,
            "extracted_candidates": 0,
            "matched_target_candidates": 0,
            "duplicate_candidates": 0,
            "retained_candidates": 0,
            "limit_reached": False,
        }

    source_limited = (
        len(parsed["candidates"])
        > max_candidates_per_source
    )

    total_limited = (
        max_candidates_remaining is not None
        and len(parsed["candidates"])
        > max_candidates_remaining
    )

    selected = parsed["candidates"][:limit]

    retained = []
    duplicates = 0
    matched = 0

    for candidate in selected:
        try:
            url = normalize_url(
                candidate["raw_url"]
            )
        except (TypeError, ValueError):
            parsed["errors"].append(
                "无效线路 URL："
                f"{candidate.get('raw_url', '')[:100]}"
            )
            continue

        if candidate["channel"] is not None:
            matched += 1

        if url in seen_urls:
            duplicates += 1
            continue

        seen_urls.add(url)

        retained.append({
            **candidate,
            "normalized_url": url,
        })

    if source_limited:
        parsed["errors"].append(
            f"达到单来源候选上限 "
            f"{max_candidates_per_source}，"
            "其余候选不再处理"
        )

    if total_limited:
        parsed["errors"].append(
            "达到本轮剩余候选额度，"
            "其余候选不再处理"
        )

    return {
        **parsed,
        "candidates": retained,
        "extracted_candidates": len(selected),
        "matched_target_candidates": matched,
        "duplicate_candidates": duplicates,
        "retained_candidates": len(retained),
        "limit_reached": source_limited,
    }
