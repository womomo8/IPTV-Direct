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
        if (parsed.scheme.lower() not in ("http", "https") or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.netloc.endswith(":")):
            return False
        port = parsed.port
        return port is None or 1 <= port <= 65535
    except ValueError:
        return False


def _source_key(value: str) -> str:
    """生成统一来源去重键，忽略片段和默认端口。"""
    if not _valid_http_url(value):
        raise ValueError("无法为无效 URL 生成去重键")
    parsed = urlsplit(value.strip())
    scheme, host = parsed.scheme.lower(), parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    netloc, port = host, parsed.port
    if port is not None and not ((scheme == "http" and port == 80)
                                 or (scheme == "https" and port == 443)):
        netloc += f":{port}"
    return urlunsplit((scheme, netloc, parsed.path or "/", parsed.query, ""))


def prepare_sources(manual_text: str, sources_config: dict[str, Any]) -> dict[str, Any]:
    """校验人工来源、内嵌 M3U 来源和启用的自动来源。"""
    if not isinstance(manual_text, str):
        raise SourceConfigError("manual.txt 内容必须是文本")
    if not isinstance(sources_config, dict):
        raise SourceConfigError("sources.json 顶层必须是 JSON 对象")
    configured = sources_config.get("sources")
    if not isinstance(configured, list):
        raise SourceConfigError("sources.json 必须包含 sources 数组")

    manual, automatic, errors, disabled = [], [], [], 0
    lines, pos = manual_text.splitlines(), 0
    while pos < len(lines):
        raw, value, number = lines[pos], lines[pos].strip(), pos + 1
        if not value:
            pos += 1
            continue
        if value.upper() == "#EXTM3U":
            block, pos, ended = [raw], pos + 1, False
            while pos < len(lines):
                line = lines[pos]
                if line.strip().upper() == "#END-M3U":
                    ended, pos = True, pos + 1
                    break
                block.append(line)
                pos += 1
            if not ended:
                errors.append(f"manual.txt 第 {number} 行：内嵌 M3U 缺少 #END-M3U 结束标记，已跳过")
                continue
            manual.append({"url": f"manual.txt#inline-m3u-{number}",
                           "source_type": "manual", "priority": 0,
                           "order": len(manual), "name": f"人工内嵌 M3U（第{number}行）",
                           "mode": "inline", "inline_content": "\n".join(block).strip() + "\n"})
            continue
        if value.startswith("#"):
            pos += 1
            continue
        if not _valid_http_url(value):
            errors.append(f"manual.txt 第 {number} 行：不是有效的 HTTP/HTTPS URL，已跳过")
            pos += 1
            continue
        manual.append({"url": value, "source_type": "manual", "priority": 0,
                       "order": len(manual), "name": f"人工来源第{number}行", "mode": "manual"})
        pos += 1

    ids = [x["id"].strip() for x in configured
           if isinstance(x, dict) and isinstance(x.get("id"), str) and x["id"].strip()]
    counts = Counter(ids)
    for index, item in enumerate(configured, 1):
        if not isinstance(item, dict):
            errors.append(f"sources.json 第 {index} 条：配置必须是 JSON 对象，已跳过")
            continue
        sid, name, url = item.get("id"), item.get("name"), item.get("url")
        enabled, mode, bad = item.get("enabled"), item.get("mode"), []
        if not isinstance(sid, str) or not sid.strip():
            bad.append("id 必须是非空文本")
        elif counts[sid.strip()] > 1:
            bad.append(f"id 重复：{sid}")
        if not isinstance(name, str) or not name.strip():
            bad.append("name 必须是非空文本")
        if not _valid_http_url(url):
            bad.append("url 必须是有效的 HTTP/HTTPS URL")
        if not isinstance(enabled, bool):
            bad.append("enabled 必须是布尔值")
        if mode not in ("playlist", "index"):
            bad.append("mode 只能是 playlist 或 index")
        if bad:
            errors.append(f"sources.json 第 {index} 条：" + "；".join(bad) + "，已跳过")
            continue
        if not enabled:
            disabled += 1
            continue
        automatic.append({"id": sid.strip(), "name": name.strip(), "url": url.strip(),
                          "enabled": True, "mode": mode, "source_type": "automatic",
                          "priority": 1, "order": len(automatic)})
    return {"manual_sources": manual, "automatic_sources": automatic,
            "disabled_sources": disabled, "errors": errors}


class _LimitedRedirectHandler(HTTPRedirectHandler):
    """限制重定向次数，并逐次校验跳转目标。"""
    def __init__(self, maximum: int):
        super().__init__()
        self.maximum = maximum

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        count = getattr(req, "_iptv_redirect_count", 0)
        if count >= self.maximum:
            raise HTTPError(req.full_url, code, "超过最大重定向次数", headers, fp)
        if not _valid_http_url(newurl):
            raise HTTPError(req.full_url, code, "重定向目标不是有效的 HTTP/HTTPS URL", headers, fp)
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None:
            redirected._iptv_redirect_count = count + 1
        return redirected


def _looks_like_ts(data: bytes) -> bool:
    """检查连续五个 188 字节 TS 同步位置。"""
    if len(data) < 188 * 4 + 1:
        return False
    return any(all(data[offset + 188 * i] == 0x47 for i in range(5))
               for offset in range(min(188, len(data) - 188 * 4)))


def download_url(url: str, timeout_seconds: int, max_bytes: int,
                 max_redirects: int, max_hls_manifest_bytes: int) -> dict[str, Any]:
    """下载来源内容；直接媒体只读取识别所需的开头数据。"""
    if not _valid_http_url(url):
        raise SourceDownloadError("来源 URL 无效")
    for value, label in ((timeout_seconds, "下载超时"), (max_bytes, "来源大小上限"),
                         (max_hls_manifest_bytes, "HLS 清单大小上限")):
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise SourceDownloadError(f"{label}参数无效")
    if not isinstance(max_redirects, int) or isinstance(max_redirects, bool) or max_redirects < 0:
        raise SourceDownloadError("重定向次数参数无效")

    request = Request(url, headers={"User-Agent": "IPTV-Direct/1.0", "Accept": "*/*",
                                    "Accept-Encoding": "identity"})
    opener, started = build_opener(_LimitedRedirectHandler(max_redirects)), time.monotonic()
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            status = getattr(response, "status", response.getcode())
            # collector 未主动发起 Range 请求，普通来源下载只接受完整 HTTP 200。
            if status != 200:
                raise SourceDownloadError(f"普通来源下载要求 HTTP 200，实际收到：{status}")
            final_url = response.geturl()
            if not _valid_http_url(final_url):
                raise SourceDownloadError("重定向后的最终 URL 无效")
            content_type = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
            first = response.read(min(8192, max_bytes + 1))
            if time.monotonic() - started > timeout_seconds:
                raise SourceDownloadError(f"来源下载超过 {timeout_seconds} 秒时间上限")
            if len(first) > max_bytes:
                raise SourceDownloadError(f"来源内容超过允许上限 {max_bytes} 字节")
            media_type = content_type.startswith(("video/", "audio/")) and "mpegurl" not in content_type
            if _looks_like_ts(first) or media_type:
                return {"url": url, "final_url": final_url, "content": first,
                        "content_type": content_type, "http_status": status,
                        "truncated_media_probe": True}
            content = bytearray(first)

            def is_hls(data):
                sample = data[:max_hls_manifest_bytes].decode("utf-8-sig", errors="ignore").upper()
                return sample.lstrip().startswith("#EXTM3U") and "#EXT-X-" in sample

            hls = is_hls(content)
            while True:
                limit = max_hls_manifest_bytes if hls else max_bytes
                if len(content) > limit:
                    label = "HLS 播放清单" if hls else "来源内容"
                    raise SourceDownloadError(f"{label}超过允许上限 {limit} 字节")
                remaining = limit + 1 - len(content)
                if remaining <= 0:
                    break
                chunk = response.read(min(8192, remaining))
                if time.monotonic() - started > timeout_seconds:
                    raise SourceDownloadError(f"来源下载超过 {timeout_seconds} 秒时间上限")
                if not chunk:
                    break
                content.extend(chunk)
                if not hls:
                    hls = is_hls(content)
            limit = max_hls_manifest_bytes if hls else max_bytes
            if len(content) > limit:
                label = "HLS 播放清单" if hls else "来源内容"
                raise SourceDownloadError(f"{label}超过允许上限 {limit} 字节")
            return {"url": url, "final_url": final_url, "content": bytes(content),
                    "content_type": content_type, "http_status": status,
                    "truncated_media_probe": False}
    except SourceDownloadError:
        raise
    except HTTPError as exc:
        raise SourceDownloadError(f"HTTP 请求失败：{exc.code} {exc.reason}") from exc
    except (URLError, HTTPException, TimeoutError, OSError, ValueError) as exc:
        raise SourceDownloadError(f"来源下载失败：{exc}") from exc


class _PlaylistLinkParser(HTMLParser):
    """只提取 HTML a 标签中的链接。"""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "a":
            href = dict(attrs).get("href")
            if isinstance(href, str) and href.strip():
                self.links.append(href.strip())


def extract_playlist_links(html_content: bytes, index_url: str, maximum: int) -> list[str]:
    """提取最多指定数量的 TXT、M3U、M3U8 链接。"""
    if not isinstance(maximum, int) or isinstance(maximum, bool) or maximum < 1:
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
    results, seen = [], set()
    for href in parser.links:
        absolute = urljoin(index_url, href)
        if not _valid_http_url(absolute) or not urlsplit(absolute).path.lower().endswith((".txt", ".m3u", ".m3u8")):
            continue
        key = _source_key(absolute)
        if key in seen:
            continue
        seen.add(key)
        results.append(absolute)
        if len(results) >= maximum:
            break
    return results


def inspect_source_structure(downloaded: dict[str, Any]) -> dict[str, Any]:
    """识别 TXT/M3U/HLS 结构和播放条目，不执行频道匹配。"""
    content = downloaded.get("content")
    if isinstance(content, bytes):
        text = content.decode("utf-8-sig", errors="replace")
    elif isinstance(content, str):
        text = content.lstrip("\ufeff")
    else:
        text = ""
    lines = [x.strip() for x in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    nonempty = [x for x in lines if x]
    base = downloaded.get("final_url") or downloaded.get("url") or ""
    if not nonempty:
        fmt, entries = "unknown", 0
    elif any(x.upper().startswith("#EXT-X-") for x in nonempty[:30]):
        fmt, entries = "hls", 0
        for line in nonempty:
            if not line.startswith("#") and _valid_http_url(urljoin(base, line)):
                entries += 1
    elif any(x.upper().startswith("#EXTM3U") for x in nonempty[:3]):
        fmt, entries, pending = "m3u", 0, False
        for line in lines:
            if not line:
                continue
            if line.upper().startswith("#EXTINF:"):
                pending = True
            elif line.startswith("#"):
                continue
            else:
                if pending and _valid_http_url(urljoin(base, line)):
                    entries += 1
                pending = False
    else:
        fmt, entries = "txt", 0
        for line in nonempty:
            if line.startswith("#"):
                continue
            if _valid_http_url(line):
                entries += 1
            elif "," in line and _valid_http_url(line.split(",", 1)[1].strip()):
                entries += 1
    return {"structure_valid": entries > 0, "structure_format": fmt,
            "structure_entry_count": entries}

def iter_source_contents(manual_sources: list[dict[str, Any]],
                         automatic_sources: list[dict[str, Any]],
                         settings: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """按人工优先顺序采集；重复 URL 复用下载结果，但保留每个来源身份。"""
    detection, collection = settings["detection"], settings["collection"]
    timeout = detection["source_download_timeout_seconds"]
    max_bytes = detection["max_source_download_bytes"]
    redirects = detection["max_redirects"]
    max_hls = detection["max_hls_manifest_bytes"]
    max_links = collection["max_index_links_per_page"]
    max_valid = collection["max_valid_sources"]
    if not isinstance(max_valid, int) or isinstance(max_valid, bool) or max_valid < 1:
        raise ValueError("collection.max_valid_sources 必须是正整数")

    valid_count = 0
    download_cache: dict[str, tuple[dict[str, Any] | None, str | None]] = {}

    def download_source(url: str):
        """相同规范化 URL 只下载一次，后续来源复用内容但保留自己的 URL 身份。"""
        key = _source_key(url)
        cached = download_cache.get(key)

        if cached is not None:
            cached_download, cached_error = cached
            if cached_download is None:
                return None, [cached_error or "重复来源下载失败"], True
            return {**cached_download, "url": url}, [], True

        try:
            downloaded = download_url(
                url, timeout, max_bytes, redirects, max_hls
            )
        except SourceDownloadError as exc:
            error = str(exc)
            download_cache[key] = (None, error)
            return None, [error], False

        downloaded = {**downloaded, "url": url}
        cache_entry = (downloaded, None)
        download_cache[key] = cache_entry

        final_key = _source_key(downloaded["final_url"])
        download_cache.setdefault(final_key, cache_entry)

        return downloaded, [], False

    def result(source, downloaded, errors, download_reused=False):
        nonlocal valid_count

        structure = inspect_source_structure(downloaded) if downloaded is not None else {
            "structure_valid": False,
            "structure_format": "unknown",
            "structure_entry_count": 0,
        }

        if structure["structure_valid"]:
            valid_count += 1

        return {
            "source": dict(source),
            "download": downloaded,
            "errors": list(errors),
            "download_reused": download_reused,
            **structure,
            "valid_source_count": valid_count,
            "valid_source_limit": max_valid,
        }

    # 人工来源始终先于自动来源。
    for source in manual_sources:
        if valid_count >= max_valid:
            return

        if source.get("mode") == "inline":
            content = source.get("inline_content")
            if not isinstance(content, str) or not content.strip():
                yield result(source, None, ["人工内嵌 M3U 内容为空或格式无效"])
                continue

            downloaded = {
                "url": source["url"],
                "final_url": source["url"],
                "content": content.encode("utf-8"),
                "content_type": "application/vnd.apple.mpegurl",
                "http_status": "LOCAL",
                "truncated_media_probe": False,
            }
            yield result(source, downloaded, [])
            continue

        downloaded, errors, reused = download_source(source["url"])
        yield result(source, downloaded, errors, reused)

    # 自动来源严格按 sources.json 配置顺序处理。
    for source in automatic_sources:
        if valid_count >= max_valid:
            return

        downloaded, errors, reused = download_source(source["url"])

        if downloaded is None:
            yield result(source, None, errors, reused)
            continue

        if source["mode"] == "playlist":
            yield result(source, downloaded, [], reused)
            continue

        # index 只负责提取明确的播放列表链接，不递归。
        try:
            links = extract_playlist_links(
                downloaded["content"],
                downloaded["final_url"],
                max_links,
            )
        except (ValueError, TypeError) as exc:
            yield result(source, None, [f"索引页面解析失败：{exc}"], reused)
            continue

        if not links:
            yield result(
                source,
                None,
                ["索引页面没有发现符合要求的播放列表链接"],
                reused,
            )
            continue

        for link_index, playlist_url in enumerate(links, 1):
            if valid_count >= max_valid:
                return

            discovered = {
                **source,
                "url": playlist_url,
                "mode": "playlist",
                "discovered_from": downloaded["final_url"],
                "discovered_order": link_index,
            }

            playlist_download, playlist_errors, playlist_reused = (
                download_source(playlist_url)
            )

            yield result(
                discovered,
                playlist_download,
                playlist_errors,
                playlist_reused,
            )
