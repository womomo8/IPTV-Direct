
"""来源内容识别、订阅条目解析及频道精确匹配基础功能。"""

import re
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import urlsplit


class ChannelConfigError(ValueError):
    """频道配置错误。"""


def parse_channels(lines: Iterable[str]) -> list[str]:
    """解析每行一个标准频道名称的配置文本。"""
    channels = []
    seen = set()

    for line_number, raw_line in enumerate(lines, start=1):
        name = raw_line.strip()

        if not name or name.startswith("#"):
            continue

        if name in seen:
            raise ChannelConfigError(
                f"channels.txt 第 {line_number} 行：频道名称重复：{name}"
            )

        seen.add(name)
        channels.append(name)

    if not channels:
        raise ChannelConfigError(
            "channels.txt 没有配置任何有效频道名称。"
        )

    return channels


def load_channels(path: str = "channels.txt") -> list[str]:
    """读取并解析频道配置文件。"""
    channel_path = Path(path)

    if not channel_path.is_file():
        raise ChannelConfigError(
            f"找不到频道配置文件：{channel_path}"
        )

    try:
        with channel_path.open("r", encoding="utf-8-sig") as file:
            return parse_channels(file)

    except UnicodeDecodeError as exc:
        raise ChannelConfigError(
            f"频道配置文件编码错误：{channel_path}"
        ) from exc

    except OSError as exc:
        raise ChannelConfigError(
            f"读取频道配置文件失败：{channel_path}，{exc}"
        ) from exc


def match_channel(
    candidate_name: str,
    channels: list[str],
    aliases: Optional[Mapping[str, str]] = None,
) -> Optional[str]:
    """精确匹配频道；仅使用调用方明确提供的别名映射。"""
    if not isinstance(candidate_name, str):
        return None

    name = candidate_name.strip()

    if not name:
        return None

    if name in channels:
        return name

    if aliases is not None:
        target = aliases.get(name)

        if isinstance(target, str) and target in channels:
            return target

    return None


def match_channels(
    candidate_names: Iterable[str],
    channels: list[str],
    aliases: Optional[Mapping[str, str]] = None,
) -> list[tuple[str, Optional[str]]]:
    """批量精确匹配，保留原始名称及匹配结果。"""
    return [
        (candidate, match_channel(candidate, channels, aliases))
        for candidate in candidate_names
    ]


def _is_http_url(value: str) -> bool:
    if (
        not isinstance(value, str)
        or not value
        or any(ch.isspace() for ch in value)
    ):
        return False

    try:
        parsed = urlsplit(value)

        if (
            parsed.scheme.lower() not in ("http", "https")
            or not parsed.hostname
        ):
            return False

        if parsed.port is not None and not (1 <= parsed.port <= 65535):
            return False

        return True

    except ValueError:
        return False


def _is_ts_data(data: bytes) -> bool:
    if len(data) < 188 * 4 + 1:
        return False

    for offset in range(min(188, len(data))):
        if all(
            offset + 188 * index < len(data)
            and data[offset + 188 * index] == 0x47
            for index in range(5)
        ):
            return True

    return False


def _parse_extinf(line: str) -> tuple[dict[str, str], str]:
    """提取 EXTINF 属性和逗号后的显示名称。"""
    metadata = {}
    body = line[len("#EXTINF:"):]

    if "," in body:
        attributes_text, display_name = body.split(",", 1)
    else:
        attributes_text, display_name = body, ""

    for match in re.finditer(
        r'([A-Za-z0-9_-]+)="([^"]*)"', attributes_text
    ):
        metadata[match.group(1).lower()] = match.group(2).strip()

    return metadata, display_name.strip()


def parse_source_content(
    content: bytes,
    source: dict[str, Any],
    channels: list[str],
    content_type: str = "",
    max_hls_manifest_bytes: int = 1048576,
) -> dict[str, Any]:
    """识别来源内容并提取原始条目；不执行 URL 归一和全局去重。"""
    source_url = source.get("url", "")

    if not isinstance(content, bytes):
        raise TypeError("来源内容必须是 bytes")

    if _is_ts_data(content):
        return {
            "format": "direct_media",
            "recognized": True,
            "candidates": [{
                "raw_url": source_url,
                "channel_name": None,
                "channel": None,
                "metadata": {},
            }],
            "errors": [],
        }

    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = content.decode("gb18030")
        except UnicodeDecodeError:
            if content_type.startswith(("video/", "audio/")):
                return {
                    "format": "direct_media",
                    "recognized": True,
                    "candidates": [{
                        "raw_url": source_url,
                        "channel_name": None,
                        "channel": None,
                        "metadata": {},
                    }],
                    "errors": [],
                }

            return {
                "format": "unknown",
                "recognized": False,
                "candidates": [],
                "errors": [
                    "内容不是可识别的 UTF-8/GB18030 文本或 TS 媒体数据"
                ],
            }

    stripped = text.lstrip()

    if not stripped:
        return {
            "format": "unknown",
            "recognized": False,
            "candidates": [],
            "errors": ["来源内容为空"],
        }

    upper = stripped.upper()

    # HLS M3U8：必须根据内容中的 EXT-X 标签识别。
    if upper.startswith("#EXTM3U") and any(
        line.strip().upper().startswith("#EXT-X-")
        for line in stripped.splitlines()
    ):
        if len(content) > max_hls_manifest_bytes:
            return {
                "format": "hls",
                "recognized": False,
                "candidates": [],
                "errors": [
                    f"HLS 播放清单超过 {max_hls_manifest_bytes} 字节上限"
                ],
            }

        return {
            "format": "hls",
            "recognized": True,
            "candidates": [{
                "raw_url": source_url,
                "channel_name": None,
                "channel": None,
                "metadata": {},
            }],
            "errors": [],
        }

    # 普通 M3U 频道订阅。
    if upper.startswith("#EXTM3U"):
        lines = stripped.splitlines()
        candidates = []
        pending = None

        for line in lines[1:]:
            value = line.strip()

            if not value:
                continue

            if value.startswith("#EXTINF:"):
                metadata, display_name = _parse_extinf(value)

                possible_names = [
                    metadata.get("tvg-name"),
                    metadata.get("tvg-id"),
                    display_name,
                ]
                possible_names = [
                    name for name in possible_names
                    if isinstance(name, str) and name.strip()
                ]

                # 只接受精确匹配，不进行相似度猜测。
                channel_name = next(
                    (
                        name for name in possible_names
                        if match_channel(name, channels) is not None
                    ),
                    possible_names[0] if possible_names else None,
                )

                pending = (channel_name, metadata)
                continue

            if value.startswith("#"):
                continue

            if _is_http_url(value):
                channel_name, metadata = pending or (None, {})

                candidates.append({
                    "raw_url": value,
                    "channel_name": channel_name,
                    "channel": match_channel(channel_name, channels),
                    "metadata": metadata,
                })

                pending = None

        return {
            "format": "m3u",
            "recognized": True,
            "candidates": candidates,
            "errors": [],
        }

    # TXT：支持单列 URL，以及“频道名称,URL”格式。
    candidates = []
    errors = []

    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        value = raw_line.strip()

        if not value or value.startswith("#"):
            continue

        channel_name = None
        url = value

        if "," in value:
            possible_name, possible_url = value.split(",", 1)

            if _is_http_url(possible_url.strip()):
                channel_name = possible_name.strip()
                url = possible_url.strip()

        if not _is_http_url(url):
            errors.append(
                f"第 {line_number} 行不是有效 HTTP/HTTPS URL，已跳过"
            )
            continue

        candidates.append({
            "raw_url": url,
            "channel_name": channel_name,
            "channel": match_channel(channel_name, channels),
            "metadata": {},
        })

    if candidates:
        return {
            "format": "txt",
            "recognized": True,
            "candidates": candidates,
            "errors": errors,
        }

    if (
        content_type.startswith(("video/", "audio/"))
        and "mpegurl" not in content_type
    ):
        return {
            "format": "direct_media",
            "recognized": True,
            "candidates": [{
                "raw_url": source_url,
                "channel_name": None,
                "channel": None,
                "metadata": {},
            }],
            "errors": errors,
        }

    return {
        "format": "unknown",
        "recognized": False,
        "candidates": [],
        "errors": errors or ["无法识别来源内容格式"],
    }
