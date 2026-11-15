
"""人工来源与自动来源配置读取。"""

from collections import Counter
from typing import Any
from urllib.parse import urlsplit


class SourceConfigError(ValueError):
    """来源配置整体结构错误。"""


def _valid_http_url(value: Any) -> bool:
    """只接受包含主机名的 HTTP 或 HTTPS URL。"""
    if not isinstance(value, str):
        return False

    value = value.strip()
    if not value or any(character.isspace() for character in value):
        return False

    try:
        parsed = urlsplit(value)

        if parsed.scheme.lower() not in ("http", "https"):
            return False

        if not parsed.hostname:
            return False

        if parsed.port is not None and not (1 <= parsed.port <= 65535):
            return False

    except ValueError:
        return False

    return True


def prepare_sources(
    manual_text: str,
    sources_config: dict[str, Any],
) -> dict[str, Any]:
    """校验并整理人工来源和启用的自动来源，不执行网络下载。"""

    if not isinstance(manual_text, str):
        raise SourceConfigError("manual.txt 内容必须是文本")

    if not isinstance(sources_config, dict):
        raise SourceConfigError("sources.json 顶层必须是 JSON 对象")

    configured_sources = sources_config.get("sources")

    if not isinstance(configured_sources, list):
        raise SourceConfigError("sources.json 必须包含 sources 数组")

    manual_sources = []
    errors = []

    for line_number, raw_line in enumerate(
        manual_text.splitlines(), start=1
    ):
        url = raw_line.strip()

        if not url or url.startswith("#"):
            continue

        if not _valid_http_url(url):
            errors.append(
                f"manual.txt 第 {line_number} 行："
                "不是有效的 HTTP/HTTPS URL，已跳过"
            )
            continue

        manual_sources.append({
            "url": url,
            "source_type": "manual",
            "priority": 0,
            "order": len(manual_sources),
        })

    ids = [
        item.get("id").strip()
        for item in configured_sources
        if isinstance(item, dict)
        and isinstance(item.get("id"), str)
        and item.get("id").strip()
    ]

    id_counts = Counter(ids)

    automatic_sources = []
    disabled_count = 0

    for index, item in enumerate(configured_sources, start=1):
        if not isinstance(item, dict):
            errors.append(
                f"sources.json 第 {index} 条："
                "配置必须是 JSON 对象，已跳过"
            )
            continue

        source_id = item.get("id")
        name = item.get("name")
        url = item.get("url")
        enabled = item.get("enabled")
        mode = item.get("mode")

        item_errors = []

        if not isinstance(source_id, str) or not source_id.strip():
            item_errors.append("id 必须是非空文本")
        elif id_counts[source_id.strip()] > 1:
            item_errors.append(f"id 重复：{source_id}")

        if not isinstance(name, str) or not name.strip():
            item_errors.append("name 必须是非空文本")

        if not _valid_http_url(url):
            item_errors.append("url 必须是有效的 HTTP/HTTPS URL")

        if not isinstance(enabled, bool):
            item_errors.append("enabled 必须是布尔值")

        if mode not in ("playlist", "index"):
            item_errors.append("mode 只能是 playlist 或 index")

        if item_errors:
            errors.append(
                f"sources.json 第 {index} 条："
                + "；".join(item_errors)
                + "，已跳过"
            )
            continue

        if not enabled:
            disabled_count += 1
            continue

        automatic_sources.append({
            "id": source_id.strip(),
            "name": name.strip(),
            "url": url.strip(),
            "enabled": True,
            "mode": mode,
            "source_type": "automatic",
            "priority": 1,
            "order": len(automatic_sources),
        })

    return {
        "manual_sources": manual_sources,
        "automatic_sources": automatic_sources,
        "disabled_sources": disabled_count,
        "errors": errors,
    }
