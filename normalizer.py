
"""频道配置读取与精确匹配。

当前阶段职责：
1. 读取 channels.txt 中的标准频道名称。
2. 忽略空行和 # 注释。
3. 检查频道名称是否重复。
4. 根据频道标准名称执行精确匹配。
5. 无法精确匹配时返回 None，不进行模糊猜测。

后续阶段将在本文件中扩展订阅解析、URL 规范化和去重功能。
"""

from pathlib import Path
from typing import Iterable, Optional


class ChannelConfigError(ValueError):
    """频道配置错误。"""


def parse_channels(lines: Iterable[str]) -> list[str]:
    """解析频道配置文本。

    每行填写一个标准频道名称。
    空行和以 # 开头的注释行会被忽略。
    """

    channels = []
    seen = set()

    for line_number, raw_line in enumerate(lines, start=1):
        name = raw_line.strip()

        if not name or name.startswith("#"):
            continue

        if name in seen:
            raise ChannelConfigError(
                f"channels.txt 第 {line_number} 行："
                f"频道名称重复：{name}"
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
        with channel_path.open(
            "r",
            encoding="utf-8-sig"
        ) as file:
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
    channels: list[str]
) -> Optional[str]:
    """将候选频道名称与目标频道进行精确匹配。

    只有名称完全一致时才返回标准频道名称。
    不进行模糊匹配、相似度判断或自动猜测。
    """

    if not isinstance(candidate_name, str):
        return None

    name = candidate_name.strip()

    if not name:
        return None

    for channel in channels:
        if name == channel:
            return channel

    return None


def match_channels(
    candidate_names: Iterable[str],
    channels: list[str]
) -> list[tuple[str, Optional[str]]]:
    """批量匹配频道名称。

    返回：
    [
        (原始频道名称, 匹配到的标准频道名称或 None),
        ...
    ]
    """

    results = []

    for candidate in candidate_names:
        matched = match_channel(candidate, channels)
        results.append((candidate, matched))

    return results
