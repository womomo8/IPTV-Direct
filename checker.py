
"""IPTV-Direct 本地 IPv4、HTTP、HLS 与 MPEG-TS 检测。"""

from __future__ import annotations

import http.client
import re
import socket
import ssl
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlsplit

PASS, FAIL, INCOMPLETE = "PASS", "FAIL", "INCOMPLETE"


class CheckError(Exception):
    def __init__(self, status, reason, connected=False):
        super().__init__(reason)
        self.status = status
        self.connected = connected


class IPv4HTTPConnection(http.client.HTTPConnection):
    def connect(self):
        self.ipv4_connected = False
        addresses = socket.getaddrinfo(
            self.host, self.port, socket.AF_INET, socket.SOCK_STREAM
        )
        error = None
        for family, socktype, proto, _, address in addresses:
            sock = socket.socket(family, socktype, proto)
            try:
                sock.settimeout(self.timeout)
                sock.connect(address)
                self.sock = sock
                self.ipv4_connected = True
                return
            except OSError as exc:
                error = exc
                sock.close()
        raise error or OSError("没有可用的 IPv4 地址")


class IPv4HTTPSConnection(IPv4HTTPConnection):
    def __init__(self, host, port=None, *, context=None, **kwargs):
        super().__init__(host, port, **kwargs)
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
        self.budget = settings["candidate_active_budget_seconds"]
        self.max_redirects = settings["max_redirects"]
        self.manifest_limit = settings["max_hls_manifest_bytes"]
        self.ts_limit = settings["max_ts_read_bytes"]
        self.slots = {}
        self.slots_lock = threading.Lock()

    def _slot(self, host):
        host = host.lower()
        with self.slots_lock:
            if host not in self.slots:
                self.slots[host] = threading.BoundedSemaphore(
                    self.settings["per_host_concurrency"]
                )
            return self.slots[host]

    @staticmethod
    def _content_length(value, connected):
        if value is None:
            return None
        if not re.fullmatch(r"[0-9]+", value.strip()):
            raise CheckError(FAIL, "HTTP Content-Length 无效", connected)
        return int(value)

    @staticmethod
    def _content_range(value, connected, limit):
        match = re.fullmatch(r"bytes ([0-9]+)-([0-9]+)/([0-9]+)", value or "")
        if not match:
            raise CheckError(FAIL, "HTTP 206 的 Content-Range 无效", connected)

        start, end, total = map(int, match.groups())
        if start != 0 or end < start or end >= total or end >= limit:
            raise CheckError(FAIL, "HTTP 206 的范围或总长度无效", connected)
        return start, end, total

    @staticmethod
    def _read(response, conn, limit, deadline, connected):
        chunks = []
        size = 0
        while size < limit:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CheckError(
                    INCOMPLETE, "超过单候选主动检测时间预算", connected
                )

            sock = getattr(conn, "sock", None)
            if sock is not None:
                sock.settimeout(remaining)

            try:
                part = response.read(min(8192, limit - size))
            except (socket.timeout, TimeoutError) as exc:
                raise CheckError(INCOMPLETE, f"响应读取超时：{exc}", connected)
            if not part:
                break
            chunks.append(part)
            size += len(part)

        return b"".join(chunks)

    def _request(
        self, url, deadline, queue_deadline, limit,
        use_range=True, first_slot=None
    ):
        current = url
        redirects = 0
        held_slot = first_slot
        connected_any = False

        while True:
            conn = None
            slot = held_slot
            acquired = slot is not None
            held_slot = None

            try:
                parsed = urlsplit(current)
                scheme = parsed.scheme.lower()
                if (
                    scheme not in ("http", "https")
                    or not parsed.hostname
                    or parsed.username is not None
                    or parsed.password is not None
                ):
                    raise CheckError(FAIL, "只支持无凭据的 HTTP/HTTPS URL", connected_any)

                host = parsed.hostname
                if slot is None:
                    slot = self._slot(host)
                    wait = queue_deadline - time.monotonic()
                    if wait <= 0 or not slot.acquire(timeout=wait):
                        raise CheckError(
                            INCOMPLETE, "等待主机并发槽位超过 60 秒", connected_any
                        )
                    acquired = True

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CheckError(
                        INCOMPLETE, "超过单候选主动检测时间预算", connected_any
                    )

                port = parsed.port or (443 if scheme == "https" else 80)
                cls = IPv4HTTPSConnection if scheme == "https" else IPv4HTTPConnection
                options = {"timeout": remaining}
                if scheme == "https":
                    options["context"] = ssl.create_default_context()
                conn = cls(host, port, **options)

                path = parsed.path or "/"
                if parsed.query:
                    path += "?" + parsed.query

                headers = {
                    "Host": parsed.netloc,
                    "User-Agent": "IPTV-Direct/1.0",
                    "Accept": "*/*",
                    "Accept-Encoding": "identity",
                    "Connection": "close",
                }
                if use_range:
                    headers["Range"] = f"bytes=0-{limit - 1}"

                conn.request("GET", path, headers=headers)
                response = conn.getresponse()
                connected = bool(getattr(conn, "ipv4_connected", False))
                connected_any = connected_any or connected

                if response.status in (301, 302, 303, 307, 308):
                    location = response.getheader("Location")
                    if not location:
                        raise CheckError(FAIL, "重定向缺少 Location", connected_any)
                    if redirects >= self.max_redirects:
                        raise CheckError(FAIL, "超过最大重定向次数", connected_any)
                    current = urljoin(current, location)
                    redirects += 1
                    continue

                if response.status not in (200, 206):
                    status = FAIL if response.status < 500 else INCOMPLETE
                    raise CheckError(
                        status, f"HTTP 状态码 {response.status}", connected_any
                    )

                content_range = response.getheader("Content-Range", "")
                content_length = self._content_length(
                    response.getheader("Content-Length"), connected_any
                )

                range_info = None
                if response.status == 206:
                    range_info = self._content_range(
                        content_range, connected_any, limit
                    )
                    expected = range_info[1] - range_info[0] + 1
                    if content_length is not None and content_length != expected:
                        raise CheckError(
                            FAIL, "HTTP 206 的 Content-Length 与范围不一致",
                            connected_any
                        )
                else:
                    expected = content_length

                data = self._read(
                    response, conn, limit, deadline, connected_any
                )
                if not data:
                    raise CheckError(FAIL, "HTTP 响应没有实际内容", connected_any)

                if expected is not None and expected <= limit and len(data) != expected:
                    raise CheckError(
                        INCOMPLETE, "HTTP 响应正文长度与声明不一致", connected_any
                    )

                complete = False
                if response.status == 206:
                    start, end, total = range_info
                    complete = end + 1 == total and len(data) == total
                elif content_length is not None:
                    complete = content_length == len(data)
                else:
                    complete = len(data) < limit

                return {
                    "data": data,
                    "url": current,
                    "status": response.status,
                    "content_type": response.getheader("Content-Type", ""),
                    "content_length": content_length,
                    "content_range": content_range,
                    "range_total": range_info[2] if range_info else None,
                    "complete": complete,
                    "connected": connected_any,
                }

            except CheckError as exc:
                if connected_any and not exc.connected:
                    exc.connected = True
                raise
            except (socket.timeout, TimeoutError) as exc:
                raise CheckError(
                    INCOMPLETE, f"连接或读取超时：{exc}", connected_any
                )
            except (OSError, ssl.SSLError, http.client.HTTPException, ValueError) as exc:
                raise CheckError(
                    FAIL, f"IPv4/HTTP 请求失败：{exc}", connected_any
                )
            finally:
                if conn:
                    conn.close()
                if acquired:
                    slot.release()

    @staticmethod
    def _ts_ok(data):
        if len(data) < 188 * 4 + 1:
            return False
        return any(
            all(data[offset + 188 * i] == 0x47 for i in range(5))
            for offset in range(min(188, len(data) - 188 * 4))
        )

    @staticmethod
    def _playlist(data):
        try:
            lines = [
                line.strip()
                for line in data.decode("utf-8-sig").splitlines()
                if line.strip()
            ]
        except UnicodeDecodeError:
            raise CheckError(FAIL, "HLS 清单不是有效 UTF-8")

        if not lines or lines[0] != "#EXTM3U":
            raise CheckError(FAIL, "不是有效的 HLS 播放清单")

        for line in lines:
            if line.startswith(("#EXT-X-KEY:", "#EXT-X-SESSION-KEY:")):
                match = re.search(r"(?:^|,)METHOD=([^,]+)", line.upper())
                if not match or match.group(1).strip() != "NONE":
                    raise CheckError(FAIL, "不支持加密 HLS")

        if any(line.startswith("#EXT-X-MAP:") for line in lines):
            raise CheckError(FAIL, "不支持 fMP4 HLS")
        if any(line.startswith("#EXT-X-BYTERANGE:") for line in lines):
            raise CheckError(INCOMPLETE, "不支持 HLS 分片字节范围")
        if any(line.startswith("#EXT-X-PART:") for line in lines):
            raise CheckError(FAIL, "不支持低延迟 fMP4 分片")

        if not any(
            line.startswith((
                "#EXT-X-STREAM-INF:", "#EXT-X-TARGETDURATION:",
                "#EXT-X-MEDIA-SEQUENCE:", "#EXT-X-PLAYLIST-TYPE:",
            ))
            for line in lines
        ):
            raise CheckError(FAIL, "这是频道订阅 M3U，不是 HLS 播放清单")

        return lines

    def _complete_manifest(self, result, label):
        length = result["content_length"]
        total = result["range_total"]

        if length is not None and length > self.manifest_limit:
            raise CheckError(FAIL, f"{label}超过 1 MB")
        if total is not None and total > self.manifest_limit:
            raise CheckError(FAIL, f"{label}超过 1 MB")
        if len(result["data"]) > self.manifest_limit:
            raise CheckError(FAIL, f"{label}超过 1 MB")
        if not result["complete"]:
            raise CheckError(INCOMPLETE, f"{label}不完整，无法确认全部内容")

        return result["data"]

    def _manifest(self, deadline, queue_deadline, first):
        data = first["data"]
        final_url = first["url"]

        if not first["complete"]:
            full = self._request(
                final_url, deadline, queue_deadline,
                self.manifest_limit, use_range=True
            )
            data = self._complete_manifest(full, "HLS 清单")
            final_url = full["url"]
        elif len(data) > self.manifest_limit:
            raise CheckError(FAIL, "HLS 清单超过 1 MB")

        lines = self._playlist(data)

        if any(line.startswith("#EXT-X-STREAM-INF:") for line in lines):
            variants = []
            for index, line in enumerate(lines[:-1]):
                if (
                    line.startswith("#EXT-X-STREAM-INF:")
                    and not lines[index + 1].startswith("#")
                ):
                    variants.append(urljoin(final_url, lines[index + 1]))

            if not variants:
                raise CheckError(FAIL, "主播放清单没有媒体播放列表")

            media = self._request(
                variants[0], deadline, queue_deadline,
                self.manifest_limit, use_range=True
            )
            media_data = self._complete_manifest(media, "媒体播放清单")
            media_url = media["url"]
            media_lines = self._playlist(media_data)
        else:
            media_url, media_lines = final_url, lines

        segments = []
        has_extinf = False
        for line in media_lines:
            if line.startswith("#EXTINF:"):
                has_extinf = True
            elif not line.startswith("#"):
                if not has_extinf:
                    raise CheckError(FAIL, "媒体分片缺少对应的 EXTINF")
                segments.append(urljoin(media_url, line))
                has_extinf = False

        if has_extinf:
            raise CheckError(INCOMPLETE, "媒体清单最后一个 EXTINF 缺少分片地址")
        if len(segments) < 2:
            raise CheckError(INCOMPLETE, "媒体清单不足两个分片")

        for segment in segments[:3]:
            part = self._request(
                segment, deadline, queue_deadline,
                self.ts_limit, use_range=True
            )
            if not self._ts_ok(part["data"]):
                raise CheckError(FAIL, "HLS 分片不是有效 MPEG-TS 数据")

        return {
            "status": PASS,
            "reason": f"媒体清单及 {min(3, len(segments))} 个 TS 分片通过",
        }

    def check_one(self, candidate, submitted_at):
        result = {
            **candidate,
            "checks": {
                key: {"status": INCOMPLETE, "reason": "尚未检测"}
                for key in ("ipv4", "http", "hls", "ts")
            },
        }
        url = candidate.get("normalized_url")
        if not url:
            for item in result["checks"].values():
                item.update(status=FAIL, reason="缺少规范化 URL")
            return result

        try:
            parsed = urlsplit(url)
            host = parsed.hostname
            if not host or parsed.scheme.lower() not in ("http", "https"):
                raise ValueError("URL 缺少有效主机名或协议")
            if parsed.username is not None or parsed.password is not None:
                raise ValueError("URL 不允许包含用户名或密码")
            _ = parsed.port

            slot = self._slot(host)
            wait = self.settings["max_queue_seconds"] - (
                time.monotonic() - submitted_at
            )
            if wait <= 0 or not slot.acquire(timeout=wait):
                raise CheckError(INCOMPLETE, "等待主机并发槽位超过 60 秒")

            deadline = time.monotonic() + self.budget
            try:
                first = self._request(
                    url, deadline,
                    submitted_at + self.settings["max_queue_seconds"],
                    self.ts_limit, use_range=True, first_slot=slot
                )
                slot = None

                result["checks"]["ipv4"] = {
                    "status": PASS, "reason": "已建立真实 IPv4 连接"
                }
                result["checks"]["http"] = {
                    "status": PASS,
                    "reason": f"HTTP {first['status']} 且读取到实际内容",
                }

                if first["data"].lstrip().startswith(b"#EXTM3U"):
                    try:
                        hls = self._manifest(
                            deadline,
                            submitted_at + self.settings["max_queue_seconds"],
                            first,
                        )
                        result["checks"]["hls"] = hls
                        result["checks"]["ts"] = {
                            "status": PASS, "reason": hls["reason"]
                        }
                    except CheckError as exc:
                        result["checks"]["hls"] = {
                            "status": exc.status, "reason": str(exc)
                        }
                        result["checks"]["ts"] = {
                            "status": exc.status,
                            "reason": "HLS 分片检测未通过",
                        }

                elif self._ts_ok(first["data"]):
                    result["checks"]["ts"] = {
                        "status": PASS,
                        "reason": "Range 数据通过 5 个 TS 同步位置检查",
                    }
                    result["checks"]["hls"] = {
                        "status": INCOMPLETE,
                        "reason": "该线路是直接 TS，不是 HLS 清单",
                    }
                else:
                    result["checks"]["hls"] = {
                        "status": FAIL, "reason": "响应不是受支持的 HLS 清单"
                    }
                    result["checks"]["ts"] = {
                        "status": FAIL, "reason": "响应不符合 MPEG-TS 同步结构"
                    }

            except CheckError as exc:
                result["checks"]["ipv4"] = {
                    "status": PASS if exc.connected else exc.status,
                    "reason": "已建立真实 IPv4 连接" if exc.connected else str(exc),
                }
                result["checks"]["http"] = {
                    "status": exc.status, "reason": str(exc)
                }
                result["checks"]["hls"] = {
                    "status": INCOMPLETE, "reason": "前置 HTTP 检测未通过"
                }
                result["checks"]["ts"] = {
                    "status": INCOMPLETE, "reason": "未取得可检查的媒体数据"
                }
            finally:
                if slot is not None:
                    slot.release()

        except CheckError as exc:
            for item in result["checks"].values():
                item.update(status=exc.status, reason=str(exc))
        except (ValueError, TypeError) as exc:
            for item in result["checks"].values():
                item.update(status=FAIL, reason=f"候选 URL 无效：{exc}")

        return result


def check_candidates(candidates, settings):
    """按输入顺序检测候选；settings 接收 settings.json 的 detection 区段。"""
    required = (
        "local_concurrency", "per_host_concurrency", "queue_limit",
        "max_queue_seconds", "candidate_active_budget_seconds",
        "max_hls_manifest_bytes", "max_ts_read_bytes", "max_redirects",
    )
    if not isinstance(settings, dict) or any(k not in settings for k in required):
        raise ValueError("checker 缺少必要的 detection 配置")

    workers = settings["local_concurrency"]
    queue_limit = settings["queue_limit"]
    if (
        not isinstance(workers, int) or isinstance(workers, bool)
        or not isinstance(queue_limit, int) or isinstance(queue_limit, bool)
        or workers < 1 or queue_limit < 1
    ):
        raise ValueError("检测并发和队列上限必须为正整数")

    for key in (
        "per_host_concurrency", "max_queue_seconds",
        "candidate_active_budget_seconds", "max_hls_manifest_bytes",
        "max_ts_read_bytes", "max_redirects",
    ):
        value = settings[key]
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValueError(f"检测配置 {key} 必须为正整数")

    detector = Detector(settings)
    items = list(candidates)
    results = []

    for start in range(0, len(items), queue_limit):
        batch = items[start:start + queue_limit]
        submitted_at = time.monotonic()

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(detector.check_one, item, submitted_at)
                for item in batch
            ]
            for index, future in enumerate(futures):
                try:
                    results.append(future.result())
                except Exception as exc:
                    results.append({
                        **batch[index],
                        "checks": {
                            key: {
                                "status": INCOMPLETE,
                                "reason": f"检测程序异常：{exc}",
                            }
                            for key in ("ipv4", "http", "hls", "ts")
                        },
                    })

    return results
