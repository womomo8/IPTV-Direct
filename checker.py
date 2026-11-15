
"""IPTV-Direct：本地 IPv4、HTTP、HLS 与 MPEG-TS 检测。"""

from __future__ import annotations

import http.client
import socket
import ssl
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlsplit


PASS = "PASS"
FAIL = "FAIL"
INCOMPLETE = "INCOMPLETE"


class CheckError(Exception):
    def __init__(self, status, reason, connected=False):
        super().__init__(reason)
        self.status = status
        self.connected = connected


class IPv4HTTPConnection(http.client.HTTPConnection):
    """强制 IPv4 连接，HTTP Host 仍使用原域名。"""

    def connect(self):
        addresses = socket.getaddrinfo(
            self.host, self.port, socket.AF_INET, socket.SOCK_STREAM
        )
        last_error = None

        for family, socktype, proto, _, address in addresses:
            sock = socket.socket(family, socktype, proto)
            try:
                sock.settimeout(self.timeout)
                sock.connect(address)
                self.sock = sock
                return
            except OSError as exc:
                last_error = exc
                sock.close()

        raise last_error or OSError("没有可用的 IPv4 地址")


class IPv4HTTPSConnection(IPv4HTTPConnection):
    """强制 IPv4，同时保留 TLS SNI 和证书验证。"""

    def __init__(
        self, host, port=None, *,
        timeout=socket._GLOBAL_DEFAULT_TIMEOUT,
        context=None, **kwargs
    ):
        super().__init__(host, port, timeout=timeout, **kwargs)
        self.context = context or ssl.create_default_context()

    def connect(self):
        super().connect()
        try:
            self.sock = self.context.wrap_socket(
                self.sock, server_hostname=self.host
            )
        except Exception:
            self.sock.close()
            self.sock = None
            raise


class Detector:
    def __init__(self, settings):
        self.settings = settings
        self.timeout = settings["candidate_active_budget_seconds"]
        self.max_redirects = settings["max_redirects"]
        self.max_manifest = settings["max_hls_manifest_bytes"]
        self.max_ts = settings["max_ts_read_bytes"]
        self.host_slots = {}
        self.host_lock = threading.Lock()

    def _host_slot(self, host):
        with self.host_lock:
            return self.host_slots.setdefault(
                host,
                threading.BoundedSemaphore(
                    self.settings["per_host_concurrency"]
                ),
            )

    def _request(self, url, deadline, limit, range_request=False):
        current = url
        redirects = 0

        while True:
            parsed = urlsplit(current)
            scheme = parsed.scheme.lower()

            if scheme not in ("http", "https") or not parsed.hostname:
                raise CheckError(FAIL, "只支持有效的 HTTP/HTTPS URL")

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CheckError(
                    INCOMPLETE, "超过单候选检测总时间预算"
                )

            host = parsed.hostname
            port = parsed.port or (443 if scheme == "https" else 80)
            connection_type = (
                IPv4HTTPSConnection
                if scheme == "https"
                else IPv4HTTPConnection
            )

            try:
                if scheme == "https":
                    conn = connection_type(
                        host,
                        port,
                        timeout=remaining,
                        context=ssl.create_default_context(),
                    )
                else:
                    conn = connection_type(
                        host, port, timeout=remaining
                    )

                path = parsed.path or "/"
                if parsed.query:
                    path += "?" + parsed.query

                headers = {
                    "Host": parsed.netloc,
                    "User-Agent": "IPTV-Direct/1.0",
                    "Accept": "*/*",
                    "Connection": "close",
                }

                if range_request:
                    headers["Range"] = f"bytes=0-{limit - 1}"

                conn.request("GET", path, headers=headers)
                response = conn.getresponse()

                if response.status in (301, 302, 303, 307, 308):
                    location = response.getheader("Location")
                    response.read(1024)
                    conn.close()

                    if not location:
                        raise CheckError(
                            FAIL, "重定向响应缺少 Location",
                            connected=True,
                        )

                    if redirects >= self.max_redirects:
                        raise CheckError(
                            FAIL, "超过最大重定向次数",
                            connected=True,
                        )

                    current = urljoin(current, location)
                    redirects += 1
                    continue

                if response.status not in (200, 206):
                    status = (
                        FAIL if response.status < 500
                        else INCOMPLETE
                    )
                    raise CheckError(
                        status,
                        f"HTTP 状态码 {response.status}",
                        connected=True,
                    )

                content_length = response.getheader("Content-Length")
                if (
                    content_length
                    and int(content_length) > limit
                ):
                    raise CheckError(
                        FAIL,
                        f"响应内容超过读取上限 {limit} 字节",
                        connected=True,
                    )

                data = response.read(limit)
                content_type = response.getheader(
                    "Content-Type", ""
                )
                final_url = current
                conn.close()

                if not data:
                    raise CheckError(
                        FAIL, "HTTP 响应没有实际内容",
                        connected=True,
                    )

                return (
                    data,
                    content_type,
                    final_url,
                    response.status,
                )

            except CheckError:
                try:
                    conn.close()
                except Exception:
                    pass
                raise

            except (socket.timeout, TimeoutError) as exc:
                try:
                    conn.close()
                except Exception:
                    pass
                raise CheckError(
                    INCOMPLETE,
                    f"连接或读取超时：{exc or 'timeout'}",
                ) from exc

            except (
                OSError, ssl.SSLError, http.client.HTTPException
            ) as exc:
                try:
                    conn.close()
                except Exception:
                    pass
                raise CheckError(
                    FAIL, f"IPv4/HTTP 连接失败：{exc}"
                ) from exc

    @staticmethod
    def _ts_ok(data):
        """检查连续五个 188 字节 TS 同步位置。"""
        if len(data) < 188 * 4 + 1:
            return False

        return any(
            all(
                data[offset + 188 * index] == 0x47
                for index in range(5)
            )
            for offset in range(
                min(188, len(data) - 188 * 4)
            )
        )

    @staticmethod
    def _parse_playlist(data):
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise CheckError(
                FAIL, "HLS 播放清单不是有效 UTF-8"
            )

        lines = [
            line.strip()
            for line in text.splitlines()
            if line.strip()
        ]

        if not lines or lines[0] != "#EXTM3U":
            raise CheckError(
                FAIL, "响应不是有效 HLS M3U8"
            )

        if any(
            line.startswith(
                ("#EXT-X-KEY:", "#EXT-X-SESSION-KEY:")
            )
            and "METHOD=NONE" not in line.upper()
            for line in lines
        ):
            raise CheckError(FAIL, "暂不支持加密 HLS")

        if any(
            line.startswith("#EXT-X-MAP:")
            for line in lines
        ):
            raise CheckError(FAIL, "暂不支持 fMP4 HLS")

        if any(
            line.startswith("#EXT-X-BYTERANGE:")
            for line in lines
        ):
            raise CheckError(
                INCOMPLETE, "暂不支持 HLS 分片字节范围"
            )

        return lines

    def _hls(self, url, deadline, first_data=None):
        if first_data is None:
            data, _, final_url, _ = self._request(
                url, deadline, self.max_manifest
            )
        else:
            data, final_url = first_data

        lines = self._parse_playlist(data)

        # 主播放清单：找到媒体播放清单地址。
        if any(
            line.startswith("#EXT-X-STREAM-INF:")
            for line in lines
        ):
            variants = []

            for index, line in enumerate(lines[:-1]):
                if line.startswith("#EXT-X-STREAM-INF:"):
                    next_url = lines[index + 1]
                    if not next_url.startswith("#"):
                        variants.append(
                            urljoin(final_url, next_url)
                        )

            if not variants:
                raise CheckError(
                    FAIL, "主播放清单没有可用媒体播放列表"
                )

            media_data, _, media_final, _ = self._request(
                variants[0], deadline, self.max_manifest
            )
            media_lines = self._parse_playlist(media_data)

        else:
            media_final = final_url
            media_lines = lines

        segments = [
            urljoin(media_final, line)
            for line in media_lines
            if not line.startswith("#")
        ]

        if
