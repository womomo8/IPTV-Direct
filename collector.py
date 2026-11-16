"""IPTV-Direct：人工来源、自动来源采集及下载。"""

from collections import Counter
from html.parser import HTMLParser
import socket
import threading
import time
from typing import Any, Iterator
from http.client import (
    HTTPConnection,
    HTTPException,
    HTTPSConnection,
)
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import (
    HTTPHandler,
    HTTPSHandler,
    HTTPRedirectHandler,
    Request,
    build_opener,
)


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
        scheme,
        netloc,
        parsed.path or "/",
        parsed.query,
        "",
    ))


def prepare_sources(
    manual_text: str,
    sources_config: dict[str, Any],
) -> dict[str, Any]:
    """校验并整理人工来源和启用的自动来源。"""
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

    for line_number, raw_line in enumerate(
        manual_text.splitlines(),
        start=1,
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
            "name": f"人工来源第{line_number}行",
            "mode": "manual",
        })

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


def _remaining_seconds(deadline: float) -> float:
    """计算统一下载截止时间的剩余秒数。"""
    remaining = deadline - time.monotonic()

    if remaining <= 0:
        raise TimeoutError("来源下载超过总时间上限")

    return remaining


class _DeadlineConnectionMixin:
    """让连接、请求和响应正文共用同一个截止时间。"""

    def __init__(self, host, deadline, **kwargs):
        self.deadline = deadline
        self._deadline_socket = None
        self._deadline_timer = None
        super().__init__(host, **kwargs)

    def _apply_deadline(self):
        remaining = _remaining_seconds(self.deadline)
        self.timeout = remaining

        if self.sock is not None:
            self.sock.settimeout(remaining)

    def _abort_connection(self):
        """截止时间到达时主动关闭底层连接。"""
        sock = self._deadline_socket or self.sock

        if sock is None:
            return

        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

        try:
            sock.close()
        except OSError:
            pass

    def _start_deadline_timer(self):
        if self._deadline_timer is not None:
            return

        delay = _remaining_seconds(self.deadline)

        self._deadline_timer = threading.Timer(
            delay,
            self._abort_connection,
        )
        self._deadline_timer.daemon = True
        self._deadline_timer.start()

    def cancel_deadline(self):
        """下载结束后取消截止计时器。"""
        if self._deadline_timer is not None:
            self._deadline_timer.cancel()
            self._deadline_timer = None

    def connect(self):
        self._apply_deadline()
        super().connect()

        self._deadline_socket = self.sock
        self._start_deadline_timer()
        self._apply_deadline()

    def request(self, *args, **kwargs):
        self._apply_deadline()
        super().request(*args, **kwargs)

    def getresponse(self):
        self._apply_deadline()

        try:
            response = super().getresponse()
        except Exception:
            self.cancel_deadline()
            raise

        response._iptv_deadline_connection = self
        return response


class _DeadlineHTTPConnection(
    _DeadlineConnectionMixin,
    HTTPConnection,
):
    pass


class _DeadlineHTTPSConnection(
    _DeadlineConnectionMixin,
    HTTPSConnection,
):
    pass


class _DeadlineHTTPHandler(HTTPHandler):
    def __init__(self, deadline):
        super().__init__()
        self.deadline = deadline

    def http_open(self, req):
        return self.do_open(
            lambda host, **kwargs: _DeadlineHTTPConnection(
                host,
                self.deadline,
                **kwargs,
            ),
            req,
        )


class _DeadlineHTTPSHandler(HTTPSHandler):
    def __init__(self, deadline):
        super().__init__()
        self.deadline = deadline

    def https_open(self, req):
        return self.do_open(
            lambda host, **kwargs: _DeadlineHTTPSConnection(
                host,
                self.deadline,
                **kwargs,
            ),
            req,
            context=self._context,
            check_hostname=self._check_hostname,
        )


class _LimitedRedirectHandler(HTTPRedirectHandler):
    """限制重定向次数，并逐次校验跳转目标。"""

    def __init__(self, maximum: int, deadline: float):
        super().__init__()
        self.maximum = maximum
        self.deadline = deadline

    def redirect_request(
        self,
        req,
        fp,
        code,
        msg,
        headers,
        newurl,
    ):
        _remaining_seconds(self.deadline)

        count = getattr(req, "_iptv_redirect_count", 0)

        if count >= self.maximum:
            raise HTTPError(
                req.full_url,
                code,
                "超过最大重定向次数",
                headers,
                fp,
            )

        if not _valid_http_url(newurl):
            raise HTTPError(
                req.full_url,
                code,
                "重定向目标不是有效的 HTTP/HTTPS URL",
                headers,
                fp,
            )

        redirected = super().redirect_request(
            req,
            fp,
            code,
            msg,
            headers,
            newurl,
        )

        if redirected is not None:
            redirected._iptv_redirect_count = count + 1

        return redirected


def _set_response_timeout(response, deadline: float):
    """通过连接对象保存的 socket 设置响应读取超时。"""
    remaining = _remaining_seconds(deadline)

    response_file = getattr(response, "fp", response)
    connection = getattr(
        response_file,
        "_iptv_deadline_connection",
        None,
    )

    raw_socket = getattr(
        connection,
        "_deadline_socket",
        None,
    )

    if raw_socket is None:
        raise SourceDownloadError(
            "无法获取响应连接的底层 socket"
        )

    raw_socket.settimeout(remaining)


def _read_response(response, size: int, deadline: float) -> bytes:
    """每次只读取一段数据，并检查统一截止时间。"""
    _set_response_timeout(response, deadline)

    data = response.read1(size)

    _remaining_seconds(deadline)
    return data


def _looks_like_ts(data: bytes) -> bool:
    """检查连续五个 188 字节 TS 同步位置。"""
    if len(data) < 188 * 4 + 1:
        return False

    return any(
        all(
            data[offset + 188 * index] == 0x47
            for index in range(5)
        )
        for offset in range(min(188, len(data) - 188 * 4))
    )


def download_url(
    url: str,
    timeout_seconds: int,
    max_bytes: int,
    max_redirects: int,
    max_hls_manifest_bytes: int,
) -> dict[str, Any]:
    """下载来源内容，整个过程共用一个总时间预算。"""
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

    deadline = time.monotonic() + timeout_seconds

    request = Request(
        url,
        headers={
            "User-Agent": "IPTV-Direct/1.0",
            "Accept": "*/*",
            "Accept-Encoding": "identity",
        },
    )

    opener = build_opener(
        _DeadlineHTTPHandler(deadline),
        _DeadlineHTTPSHandler(deadline),
        _LimitedRedirectHandler(max_redirects, deadline),
    )

    try:
        with opener.open(
            request,
            timeout=timeout_seconds,
        ) as response:
            response_file = getattr(response, "fp", response)
            connection = getattr(
                response_file,
                "_iptv_deadline_connection",
                None,
            )

            try:
                _remaining_seconds(deadline)

                status = getattr(
                    response,
                    "status",
                    response.getcode(),
                )

                if status not in (200, 206):
                    raise SourceDownloadError(
                        f"HTTP 状态不符合要求：{status}"
                    )

                final_url = response.geturl()

                if not _valid_http_url(final_url):
                    raise SourceDownloadError(
                        "重定向后的最终 URL 无效"
                    )

                content_type = (
                    response.headers.get("Content-Type", "")
                    .split(";")[0]
                    .strip()
                    .lower()
                )

                first = _read_response(
                    response,
                    min(8192, max_bytes + 1),
                    deadline,
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
                        "utf-8-sig",
                        errors="ignore",
                    ).upper()

                    return (
                        text.lstrip().startswith("#EXTM3U")
                        and "#EXT-X-" in text
                    )

                hls_detected = is_hls_manifest(content)

                while True:
                    _remaining_seconds(deadline)

                    content_limit = (
                        max_hls_manifest_bytes
                        if hls_detected
                        else max_bytes
                    )

                    if len(content) > content_limit:
                        label = (
                            "HLS 播放清单"
                            if hls_detected
                            else "来源内容"
                        )
                        raise SourceDownloadError(
                            f"{label}超过允许上限 {content_limit} 字节"
                        )

                    remaining = content_limit + 1 - len(content)

                    if remaining <= 0:
                        break

                    chunk = _read_response(
                        response,
                        min(8192, remaining),
                        deadline,
                    )

                    if not chunk:
                        break

                    content.extend(chunk)

                    if not hls_detected:
                        hls_detected = is_hls_manifest(content)

                _remaining_seconds(deadline)

                content_limit = (
                    max_hls_manifest_bytes
                    if hls_detected
                    else max_bytes
                )

                if len(content) > content_limit:
                    label = (
                        "HLS 播放清单"
                        if hls_detected
                        else "来源内容"
                    )
                    raise SourceDownloadError(
                        f"{label}超过允许上限 {content_limit} 字节"
                    )

                return {
                    "url": url,
                    "final_url": final_url,
                    "content": bytes(content),
                    "content_type": content_type,
                    "http_status": status,
                    "truncated_media_probe": False,
                }

            finally:
                if connection is not None:
                    connection.cancel_deadline()

    except SourceDownloadError:
        raise

    except HTTPError as exc:
        connection = getattr(
            getattr(exc, "fp", None),
            "_iptv_deadline_connection",
            None,
        )

        if connection is not None:
            connection.cancel_deadline()

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
        raise SourceDownloadError(
            f"来源下载失败：{exc}"
        ) from exc


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
    """按人工优先顺序逐条下载，供 main.py 逐条处理。"""
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
        key = _source_key(url)

        if key in seen_urls:
            continue

        seen_urls.add(key)

        try:
            downloaded = download_url(
                url,
                timeout,
                max_bytes,
                max_redirects,
                max_hls_bytes,
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
                url,
                timeout,
                max_bytes,
                max_redirects,
                max_hls_bytes,
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
