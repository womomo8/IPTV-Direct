
"""人工来源与自动来源采集、下载及索引链接发现。"""

from collections import Counter
from html.parser import HTMLParser
from http.client import HTTPException
import time
from typing import Any, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class SourceConfigError(ValueError):
    """来源配置整体结构错误。"""


class SourceDownloadError(RuntimeError):
    """单个来源下载或读取失败。"""


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

    for line_number, raw_line in enumerate(manual_text.splitlines(), start=1):
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
            "name": f"人工来源第{line_number}行",
            "mode": "manual",
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


class _LimitedRedirectHandler(HTTPRedirectHandler):
    """限制单次请求的重定向次数。"""

    def __init__(self, maximum: int):
        super().__init__()
        self.maximum = maximum

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        count = getattr(req, "_iptv_redirect_count", 0)
        if count >= self.maximum:
            raise HTTPError(
                req.full_url, code, "超过最大重定向次数", headers, fp
            )

        redirected = super().redirect_request(
            req, fp, code, msg, headers, newurl
        )
        if redirected is not None:
            redirected._iptv_redirect_count = count + 1

        return redirected


def _looks_like_ts(data: bytes) -> bool:
    """检查数据开头是否存在连续五个 188 字节 TS 同步位置。"""
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


def download_url(
    url: str,
    timeout_seconds: int,
    max_bytes: int,
    max_redirects: int,
    max_hls_manifest_bytes: int,
) -> dict[str, Any]:
    """下载来源内容；媒体流只读取足够识别格式的开头数据。"""
    if not _valid_http_url(url):
        raise SourceDownloadError("来源 URL 无效")

    request = Request(
        url,
        headers={
            "User-Agent": "IPTV-Direct/1.0",
            "Accept": "*/*",
            "Accept-Encoding": "identity",
        },
    )
    opener = build_opener(_LimitedRedirectHandler(max_redirects))
    started_at = time.monotonic()

    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            content_type = (
                response.headers.get("Content-Type", "")
                .split(";")[0]
                .strip()
                .lower()
            )
            status = getattr(response, "status", response.getcode())
            final_url = response.geturl()

            first = response.read(min(8192, max_bytes + 1))
            is_media_content_type = (
                content_type.startswith(("video/", "audio/"))
                and "mpegurl" not in content_type
            )

            if _looks_like_ts(first) or is_media_content_type:
                return {
                    "url": url,
                    "final_url": final_url,
                    "content": first,
                    "content_type": content_type,
                    "http_status": status,
                    "truncated_media_probe": True,
                }

            # 分块读取并持续识别 HLS，避免先把大清单完整下载后才检查上限。
            content = bytearray(first)
            first_text = first.decode("utf-8-sig", errors="ignore").upper()
            is_hls_prefix = (
                first_text.lstrip().startswith("#EXTM3U")
                and "#EXT-X-" in first_text
            )

            while True:
                content_limit = (
                    min(max_bytes, max_hls_manifest_bytes)
                    if is_hls_prefix
                    else max_bytes
                )

                if len(content) > content_limit:
                    limit_name = (
                        "HLS 播放清单"
                        if is_hls_prefix
                        else "来源内容"
                    )
                    raise SourceDownloadError(
                        f"{limit_name}超过允许上限 {content_limit} 字节"
                    )

                remaining = content_limit + 1 - len(content)
                if remaining <= 0:
                    break

                chunk = response.read(min(8192, remaining))

                if time.monotonic() - started_at > timeout_seconds:
                    raise SourceDownloadError(
                        f"来源下载超过 {timeout_seconds} 秒时间上限"
                    )

                if not chunk:
                    break

                content.extend(chunk)

                if not is_hls_prefix:
                    sniff = bytes(
                        content[:min(len(content), max_hls_manifest_bytes)]
                    )
                    sniff_text = sniff.decode(
                        "utf-8-sig", errors="ignore"
                    ).upper()

                    is_hls_prefix = (
                        sniff_text.lstrip().startswith("#EXTM3U")
                        and "#EXT-X-" in sniff_text
                    )

            if len(content) > content_limit:
                limit_name = (
                    "HLS 播放清单"
                    if is_hls_prefix
                    else "来源内容"
                )
                raise SourceDownloadError(
                    f"{limit_name}超过允许上限 {content_limit} 字节"
                )

            content = bytes(content)

            return {
                "url": url,
                "final_url": final_url,
                "content": content,
                "content_type": content_type,
                "http_status": status,
                "truncated_media_probe": False,
            }

    except SourceDownloadError:
        raise
    except HTTPError as exc:
        raise SourceDownloadError(
            f"HTTP 请求失败：{exc.code} {exc.reason}"
        ) from exc
    except (
        URLError,
        HTTPException,
        TimeoutError,
        OSError,
        ValueError,
    ) as exc:
        raise SourceDownloadError(f"来源下载失败：{exc}") from exc


class _PlaylistLinkParser(HTMLParser):
    """只提取 HTML a 标签中明确指向播放列表文件的链接。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "a":
            return

        href = dict(attrs).get("href")
        if isinstance(href, str) and href.strip():
            self.links.append(href.strip())


def extract_playlist_links(
    html_content: bytes,
    index_url: str,
    maximum: int,
) -> list[str]:
    """提取最多指定数量的 TXT、M3U、M3U8 链接，不递归访问网页。"""
    try:
        text = html_content.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = html_content.decode("gb18030")
        except UnicodeDecodeError:
            text = html_content.decode("utf-8", errors="replace")

    parser = _PlaylistLinkParser()
    parser.feed(text)
    parser.close()

    results = []
    seen = set()

    for href in parser.links:
        absolute = urljoin(index_url, href)

        if not _valid_http_url(absolute):
            continue

        path = urlsplit(absolute).path.lower()
        if not path.endswith((".txt", ".m3u", ".m3u8")):
            continue

        if absolute in seen:
            continue

        seen.add(absolute)
        results.append(absolute)

        if len(results) >= maximum:
            break

    return results


def iter_source_contents(
    manual_sources: list[dict[str, Any]],
    automatic_sources: list[dict[str, Any]],
    settings: dict[str, Any],
) -> Iterator[dict[str, Any]]:
    """按人工优先顺序逐条下载来源，供 main.py 边处理边判断停止条件。"""
    detection = settings["detection"]
    collection = settings["collection"]

    timeout = detection["source_download_timeout_seconds"]
    max_bytes = detection["max_source_download_bytes"]
    max_redirects = detection["max_redirects"]
    max_hls_bytes = detection["max_hls_manifest_bytes"]
    max_index_links = collection["max_index_links_per_page"]

    seen_urls = set()

    # 第一优先级：人工来源。
    for source in manual_sources:
        url = source["url"]

        if url in seen_urls:
            continue

        seen_urls.add(url)

        try:
            downloaded = download_url(
                url, timeout, max_bytes, max_redirects, max_hls_bytes
            )
            yield {
                "source": dict(source),
                "download": downloaded,
                "errors": [],
            }
        except SourceDownloadError as exc:
            yield {
                "source": dict(source),
                "download": None,
                "errors": [str(exc)],
            }

    # 第二优先级：自动来源。
    for source in automatic_sources:
        url = source["url"]

        if url in seen_urls:
            continue

        seen_urls.add(url)

        try:
            index_download = download_url(
                url, timeout, max_bytes, max_redirects, max_hls_bytes
            )
        except SourceDownloadError as exc:
            yield {
                "source": dict(source),
                "download": None,
                "errors": [str(exc)],
            }
            continue

        if source["mode"] == "playlist":
            yield {
                "source": dict(source),
                "download": index_download,
                "errors": [],
            }
            continue

        # index 模式：只读取当前索引页面，不递归访问普通网页。
        content = index_download["content"]

        try:
            links = extract_playlist_links(
                content,
                index_download["final_url"],
                max_index_links,
            )
        except (ValueError, TypeError) as exc:
            yield {
                "source": dict(source),
                "download": None,
                "errors": [f"索引页面解析失败：{exc}"],
            }
            continue

        if not links:
            yield {
                "source": dict(source),
                "download": None,
                "errors": ["索引页面没有发现符合要求的播放列表链接"],
            }
            continue

        for link_index, playlist_url in enumerate(links, start=1):
            if playlist_url in seen_urls:
                continue

            seen_urls.add(playlist_url)

            discovered_source = {
                **source,
                "url": playlist_url,
                "mode": "playlist",
                "discovered_from": index_download["final_url"],
                "discovered_order": link_index,
            }

            try:
                downloaded = download_url(
                    playlist_url,
                    timeout,
                    max_bytes,
                    max_redirects,
                    max_hls_bytes,
                )
                yield {
                    "source": discovered_source,
                    "download": downloaded,
                    "errors": [],
                }
            except SourceDownloadError as exc:
                yield {
                    "source": discovered_source,
                    "download": None,
                    "errors": [str(exc)],
                }
