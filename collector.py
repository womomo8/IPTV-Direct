
"""IPTV-Direct：人工来源、自动来源采集及下载。"""

from collections import Counter
from html.parser import HTMLParser
from http.client import HTTPException
import time
from typing import Any, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class SourceConfigError(ValueError):
    """来源配置整体结构错误。"""


class SourceDownloadError(RuntimeError):
    """单个来源下载或读取失败。"""


def _valid_http_url(value: Any) -> bool:
    """只接受格式正确且不含凭据的 HTTP/HTTPS URL。"""
    if not isinstance(value, str):
        return False

    value = value.strip()

    if not value or any(char.isspace() for char in value):
        return False

    try:
        parsed = urlsplit(value)

        if (
            parsed.scheme.lower() not in ("http", "https")
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.netloc.endswith(":")
        ):
            return False

        port = parsed.port
        if port is not None and not 1 <= port <= 65535:
            return False

    except ValueError:
        return False

    return True


def _source_key(value: str) -> str:
    """生成统一来源去重键，忽略片段和默认端口。"""
    if not _valid_http_url(value):
        raise ValueError("无法为无效 URL 生成去重键")

    parsed = urlsplit(value.strip())
    scheme = parsed.scheme.lower()
    host = parsed.hostname.lower()

    if ":" in host:
        host = f"[{host}]"

    netloc = host
    port = parsed.port

    if port is not None and not (
        (scheme == "http" and port == 80)
        or (scheme == "https" and port == 443)
    ):
        netloc += f":{port}"

    return urlunsplit((
        scheme, netloc, parsed.path or "/", parsed.query, ""
    ))


def prepare_sources(
    manual_text: str,
    sources_config: dict[str, Any],
) -> dict[str, Any]:
    """校验人工来源、内嵌 M3U 来源和启用的自动来源。"""
    if not isinstance(manual_text, str):
        raise SourceConfigError("manual.txt 内容必须是文本")

    if not isinstance(sources_config, dict):
        raise SourceConfigError("sources.json 顶层必须是 JSON 对象")

    configured = sources_config.get("sources")
    if not isinstance(configured, list):
        raise SourceConfigError("sources.json 必须包含 sources 数组")

    manual_sources = []
    automatic_sources = []
    errors = []
    disabled_count = 0
    lines = manual_text.splitlines()
    line_index = 0

    while line_index < len(lines):
        raw_line = lines[line_index]
        value = raw_line.strip()
        line_number = line_index + 1

        if not value:
            line_index += 1
            continue

        if value.upper() == "#EXTM3U":
            # 内嵌 M3U 从 #EXTM3U 开始，到 #END-M3U 结束。
            block = [raw_line]
            line_index += 1
            ended = False

            while line_index < len(lines):
                block_line = lines[line_index]
                if block_line.strip().upper() == "#END-M3U":
                    ended = True
                    line_index += 1
                    break
                block.append(block_line)
                line_index += 1

            if not ended:
                errors.append(
                    f"manual.txt 第 {line_number} 行："
                    "内嵌 M3U 缺少 #END-M3U 结束标记，已跳过"
                )
                continue

            m3u_text = "\n".join(block).strip() + "\n"
            manual_sources.append({
                "url": f"manual.txt#inline-m3u-{line_number}",
                "source_type": "manual",
                "priority": 0,
                "order": len(manual_sources),
                "name": f"人工内嵌 M3U（第{line_number}行）",
                "mode": "inline",
                "inline_content": m3u_text,
            })
            continue

        if value.startswith("#"):
            line_index += 1
            continue

        if not _valid_http_url(value):
            errors.append(
                f"manual.txt 第 {line_number} 行："
                "不是有效的 HTTP/HTTPS URL，已跳过"
            )
            line_index += 1
            continue

        manual_sources.append({
            "url": value,
            "source_type": "manual",
            "priority": 0,
            "order": len(manual_sources),
            "name": f"人工来源第{line_number}行",
            "mode": "manual",
        })
        line_index += 1

    ids = [
        item["id"].strip()
        for item in configured
        if (
            isinstance(item, dict)
            and isinstance(item.get("id"), str)
            and item["id"].strip()
        )
    ]
    id_counts = Counter(ids)

    for index, item in enumerate(configured, start=1):
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
    """限制重定向次数，并逐次校验跳转目标。"""

    def __init__(self, maximum: int):
        super().__init__()
        self.maximum = maximum

    def redirect_request(
        self, req, fp, code, msg, headers, newurl
    ):
        count = getattr(req, "_iptv_redirect_count", 0)

        if count >= self.maximum:
            raise HTTPError(
                req.full_url, code, "超过最大重定向次数", headers, fp
            )

        if not _valid_http_url(newurl):
            raise HTTPError(
                req.full_url, code,
                "重定向目标不是有效的 HTTP/HTTPS URL",
                headers, fp,
            )

        redirected = super().redirect_request(
            req, fp, code, msg, headers, newurl
        )

        if redirected is not None:
            redirected._iptv_redirect_count = count + 1

        return redirected


def _looks_like_ts(data: bytes) -> bool:
    """检查连续五个 188 字节 TS 同步位置。"""
    if len(data) < 188 * 4 + 1:
        return False

    return any(
        all(data[offset + 188 * index] == 0x47 for index in range(5))
        for offset in range(min(188, len(data) - 188 * 4))
    )


def download_url(
    url: str,
    timeout_seconds: int,
    max_bytes: int,
    max_redirects: int,
    max_hls_manifest_bytes: int,
) -> dict[str, Any]:
    """下载来源内容；直接媒体只读取识别所需的开头数据。"""
    if not _valid_http_url(url):
        raise SourceDownloadError("来源 URL 无效")

    for value, name in (
        (timeout_seconds, "下载超时"),
        (max_bytes, "来源大小上限"),
        (max_hls_manifest_bytes, "HLS 清单大小上限"),
    ):
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value <= 0
        ):
            raise SourceDownloadError(f"{name}参数无效")

    if (
        not isinstance(max_redirects, int)
        or isinstance(max_redirects, bool)
        or max_redirects < 0
    ):
        raise SourceDownloadError("重定向次数参数无效")

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
            status = getattr(response, "status", response.getcode())

            if status not in (200, 206):
                raise SourceDownloadError(
                    f"HTTP 状态不符合要求：{status}"
                )

            final_url = response.geturl()
            if not _valid_http_url(final_url):
                raise SourceDownloadError("重定向后的最终 URL 无效")

            content_type = (
                response.headers.get("Content-Type", "")
                .split(";")[0].strip().lower()
            )

            first = response.read(min(8192, max_bytes + 1))

            if time.monotonic() - started_at > timeout_seconds:
                raise SourceDownloadError(
                    f"来源下载超过 {timeout_seconds} 秒时间上限"
                )

            if len(first) > max_bytes:
                raise SourceDownloadError(
                    f"来源内容超过允许上限 {max_bytes} 字节"
                )

            is_media_type = (
                content_type.startswith(("video/", "audio/"))
                and "mpegurl" not in content_type
            )

            if _looks_like_ts(first) or is_media_type:
                return {
                    "url": url,
                    "final_url": final_url,
                    "content": first,
                    "content_type": content_type,
                    "http_status": status,
                    "truncated_media_probe": True,
                }

            content = bytearray(first)

            def is_hls_manifest(data: bytes) -> bool:
                sample = data[:max_hls_manifest_bytes]
                text = sample.decode(
                    "utf-8-sig", errors="ignore"
                ).upper()
                return (
                    text.lstrip().startswith("#EXTM3U")
                    and "#EXT-X-" in text
                )

            hls_detected = is_hls_manifest(content)

            while True:
                content_limit = (
                    max_hls_manifest_bytes if hls_detected else max_bytes
                )

                if len(content) > content_limit:
                    label = (
                        "HLS 播放清单" if hls_detected else "来源内容"
                    )
                    raise SourceDownloadError(
                        f"{label}超过允许上限 {content_limit} 字节"
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
                if not hls_detected:
                    hls_detected = is_hls_manifest(content)

            if len(content) > (
                max_hls_manifest_bytes if hls_detected else max_bytes
            ):
                label = (
                    "HLS 播放清单" if hls_detected else "来源内容"
                )
                limit = (
                    max_hls_manifest_bytes if hls_detected else max_bytes
                )
                raise SourceDownloadError(
                    f"{label}超过允许上限 {limit} 字节"
                )

            return {
                "url": url,
                "final_url": final_url,
                "content": bytes(content),
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
        URLError, HTTPException, TimeoutError, OSError, ValueError
    ) as exc:
        raise SourceDownloadError(f"来源下载失败：{exc}") from exc


class _PlaylistLinkParser(HTMLParser):
    """只提取 HTML a 标签中的链接。"""

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
    """提取最多指定数量的 TXT、M3U、M3U8 链接。"""
    if (
        not isinstance(maximum, int)
        or isinstance(maximum, bool)
        or maximum < 1
    ):
        raise ValueError("索引链接数量上限必须是正整数")

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

        key = _source_key(absolute)
        if key in seen:
            continue

        seen.add(key)
        results.append(absolute)
        if len(results) >= maximum:
            break

    return results


def iter_source_contents(
    manual_sources: list[dict[str, Any]],
    automatic_sources: list[dict[str, Any]],
    settings: dict[str, Any],
) -> Iterator[dict[str, Any]]:
    """按人工优先顺序逐条处理，支持本地内嵌 M3U。"""
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
        # 内嵌 M3U 已在 manual.txt 中，不需要联网下载。
        if source.get("mode") == "inline":
            content = source.get("inline_content")
            if not isinstance(content, str) or not content.strip():
                yield {
                    "source": dict(source),
                    "download": None,
                    "errors": ["人工内嵌 M3U 内容为空或格式无效"],
                }
                continue

            yield {
                "source": dict(source),
                "download": {
                    "url": source["url"],
                    "final_url": source["url"],
                    "content": content.encode("utf-8"),
                    "content_type": "application/vnd.apple.mpegurl",
                    "http_status": "LOCAL",
                    "truncated_media_probe": False,
                },
                "errors": [],
            }
            continue

        url = source["url"]
        key = _source_key(url)
        if key in seen_urls:
            continue
        seen_urls.add(key)

        try:
            downloaded = download_url(
                url, timeout, max_bytes, max_redirects, max_hls_bytes
            )
            seen_urls.add(_source_key(downloaded["final_url"]))
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
        key = _source_key(url)
        if key in seen_urls:
            continue
        seen_urls.add(key)

        try:
            index_download = download_url(
                url, timeout, max_bytes, max_redirects, max_hls_bytes
            )
            seen_urls.add(_source_key(index_download["final_url"]))
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

        try:
            links = extract_playlist_links(
                index_download["content"],
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
            key = _source_key(playlist_url)
            if key in seen_urls:
                continue
            seen_urls.add(key)

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
                seen_urls.add(_source_key(downloaded["final_url"]))
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
