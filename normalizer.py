
"""频道配置读取与精确匹配。"""

from pathlib import Path
from typing import Iterable, Mapping, Optional


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
