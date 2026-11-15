"""IPTV-Direct 本地 IPv4、HTTP、HLS 与 MPEG-TS 检测。"""

from __future__ import annotations

import http.client
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
        with self.slots_lock:
            return self.slots.setdefault(
                host,
                threading.BoundedSemaphore(
                    self.settings["per_host_concurrency"]
                ),
            )

    def _request(
        self, url, deadline, queue_deadline, limit,
        use_range=False, first_slot=None
    ):
        current = url
        redirects = 0
        held_slot = first_slot

        while True:
            conn = None
            slot = held_slot
            held_slot = None
            try:
                parsed = urlsplit(current)
                scheme = parsed.scheme.lower()
                if scheme not in ("http", "https") or not parsed.hostname:
                    raise CheckError(FAIL, "只支持有效的 HTTP/HTTPS URL")

                host = parsed.hostname
                if slot is None:
                    slot = self._slot(host)
                    wait = queue_deadline - time.monotonic()
                    if wait <= 0 or not slot.acquire(timeout=wait):
                        raise CheckError(INCOMPLETE, "等待主机并发槽位超过 60 秒")

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CheckError(INCOMPLETE, "超过单候选 8 秒主动检测预算")

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
                    "Connection": "close",
                }
                if use_range:
                    headers["Range"] = f"bytes=0-{limit - 1}"

                conn.request("GET", path, headers=headers)
                response = conn.getresponse()

                if response.status in (301, 302, 303, 307, 308):
                    location = response.getheader("Location")
                    response.read(1024)
                    connected = getattr(conn, "ipv4_connected", False)
                    if not location:
                        raise CheckError(FAIL, "重定向缺少 Location", connected)
                    if redirects >= self.max_redirects:
                        raise CheckError(FAIL, "超过最大重定向次数", connected)
                    current = urljoin(current, location)
                    redirects += 1
                    continue

                connected = getattr(conn, "ipv4_connected", False)
                if response.status not in (200, 206):
                    status = FAIL if response.status < 500 else INCOMPLETE
                    raise CheckError(status, f"HTTP 状态码 {response.status}", connected)

                content_range = response.getheader("Content-Range", "")
                if use_range and response.status == 206:
                    try:
                        unit, value = content_range.split(" ", 1)
                        bounds, total = value.split("/", 1)
                        start, end = (int(x) for x in bounds.split("-", 1))
                        if unit != "bytes" or start != 0 or end < start:
                            raise ValueError
                        if total != "*" and end >= int(total):
                            raise ValueError
                    except (ValueError, TypeError):
                        raise CheckError(FAIL, "HTTP 206 的 Content-Range 无效", connected)

                data = response.read(limit)
                if not data:
                    raise CheckError(FAIL, "HTTP 响应没有实际内容", connected)

                return {
                    "data": data,
                    "url": current,
                    "status": response.status,
                    "content_type": response.getheader("Content-Type", ""),
                    "content_length": response.getheader("Content-Length"),
                    "content_range": content_range,
                    "connected": connected,
                }

            except CheckError:
                raise
            except (socket.timeout, TimeoutError) as exc:
                connected = getattr(conn, "ipv4_connected", False) if conn else False
                raise CheckError(INCOMPLETE, f"连接或读取超时：{exc}", connected)
            except (OSError, ssl.SSLError, http.client.HTTPException, ValueError) as exc:
                connected = getattr(conn, "ipv4_connected", False) if conn else False
                raise CheckError(FAIL, f"IPv4/HTTP 请求失败：{exc}", connected)
            finally:
                if conn:
                    conn.close()
                if slot:
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

        if any(
            line.startswith(("#EXT-X-KEY:", "#EXT-X-SESSION-KEY:"))
            and "METHOD=NONE" not in line.upper()
            for line in lines
        ):
            raise CheckError(FAIL, "不支持加密 HLS")
        if any(line.startswith("#EXT-X-MAP:") for line in lines):
            raise CheckError(FAIL, "不支持 fMP4 HLS")
        if any(line.startswith("#EXT-X-BYTERANGE:") for line in lines):
            raise CheckError(INCOMPLETE, "不支持 HLS 分片字节范围")
        if not any(
            line.startswith((
                "#EXT-X-STREAM-INF:", "#EXT-X-TARGETDURATION:",
                "#EXT-X-MEDIA-SEQUENCE:", "#EXT-X-PLAYLIST-TYPE:",
            ))
            for line in lines
        ):
            raise CheckError(FAIL, "这是频道订阅 M3U，不是 HLS 媒体清单")
        return lines

    def _manifest(self, url, deadline, queue_deadline, first):
        # 初次探测使用 64 KB Range；清单超过该范围时再按 1 MB 上限读取。
        data = first["data"]
        final_url = first["url"]
        if first["status"] == 206:
            total = first["content_range"].rsplit("/", 1)[-1]
            if (total.isdigit() and int(total) > len(data)) or len(data) == self.ts_limit:
                full = self._request(
                    final_url, deadline, queue_deadline,
                    self.manifest_limit + 1, use_range=True
                )
                full_total = full["content_range"].rsplit("/", 1)[-1]
                if (full_total.isdigit() and int(full_total) > self.manifest_limit) or len(full["data"]) > self.manifest_limit:
                    raise CheckError(FAIL, "HLS 清单超过 1 MB")
                data = full["data"]
                final_url = full["url"]
        elif first["content_length"]:
            try:
                size = int(first["content_length"])
                if size > self.manifest_limit:
                    raise CheckError(FAIL, "HLS 清单超过 1 MB")
                if size > len(data) or len(data) == self.ts_limit:
                    full = self._request(
                        final_url, deadline, queue_deadline,
                        self.manifest_limit + 1, use_range=True
                    )
                    if len(full["data"]) > self.manifest_limit:
                        raise CheckError(FAIL, "HLS 清单超过 1 MB")
                    data, final_url = full["data"], full["url"]
            except ValueError:
                raise CheckError(FAIL, "HTTP Content-Length 无效")

        if len(data) > self.manifest_limit:
            raise CheckError(FAIL, "HLS 清单超过 1 MB")
        if len(data) == self.manifest_limit:
            raise CheckError(INCOMPLETE, "HLS 清单可能超过 1 MB，无法完整验证")
        lines = self._playlist(data)
        if any(line.startswith("#EXT-X-STREAM-INF:") for line in lines):
            variants = []
            for i, line in enumerate(lines[:-1]):
                if line.startswith("#EXT-X-STREAM-INF:") and not lines[i + 1].startswith("#"):
                    variants.append(urljoin(final_url, lines[i + 1]))
            if not variants:
                raise CheckError(FAIL, "主播放清单没有媒体播放列表")
            media = self._request(
                variants[0], deadline, queue_deadline,
                self.manifest_limit, use_range=True
            )
            if media["content_range"].rsplit("/", 1)[-1].isdigit() and int(
                media["content_range"].rsplit("/", 1)[-1]
            ) > self.manifest_limit:
                raise CheckError(FAIL, "媒体播放清单超过 1 MB")
            media_url, media_data = media["url"], media["data"]
            media_lines = self._playlist(media_data)
        else:
            media_url, media_lines = final_url, lines

        segments = [
            urljoin(media_url, line)
            for line in media_lines
            if not line.startswith("#")
        ]
        if len(segments) < 2:
            raise CheckError(INCOMPLETE, "媒体清单不足两个分片")

        for segment in segments[:3]:
            part = self._request(
                segment, deadline, queue_deadline,
                self.ts_limit, use_range=True
            )
            if not self._ts_ok(part["data"]):
                raise CheckError(FAIL, "HLS 分片不是有效 MPEG-TS 数据")
        return {"status": PASS, "reason": f"媒体清单及 {min(3, len(segments))} 个 TS 分片通过"}

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
            host = urlsplit(url).hostname
            if not host:
                raise ValueError("URL 缺少主机名")
            slot = self._slot(host)
            wait = self.settings["max_queue_seconds"] - (time.monotonic() - submitted_at)
            if wait <= 0 or not slot.acquire(timeout=wait):
                raise CheckError(INCOMPLETE, "等待主机并发槽位超过 60 秒")

            deadline = time.monotonic() + self.budget
            try:
                held_slot, slot = slot, None
                first = self._request(
                    url, deadline,
                    submitted_at + self.settings["max_queue_seconds"],
                    self.ts_limit, use_range=True, first_slot=held_slot
                )
                result["checks"]["ipv4"] = {
                    "status": PASS, "reason": "已建立真实 IPv4 连接"
                }
                result["checks"]["http"] = {
                    "status": PASS, "reason": f"HTTP {first['status']} 且读取到实际内容"
                }

                if first["data"].lstrip().startswith(b"#EXTM3U"):
                    try:
                        hls = self._manifest(
                            first["url"], deadline,
                            submitted_at + self.settings["max_queue_seconds"],
                            first
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
                            "status": exc.status, "reason": "HLS 分片检测未通过"
                        }
                elif self._ts_ok(first["data"]):
                    result["checks"]["ts"] = {
                        "status": PASS, "reason": "Range 数据通过 5 个 TS 同步位置检查"
                    }
                    result["checks"]["hls"] = {
                        "status": INCOMPLETE, "reason": "该线路是直接 TS，不是 HLS 清单"
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
                if slot:
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
    workers, queue_limit = settings["local_concurrency"], settings["queue_limit"]
    if not isinstance(workers, int) or not isinstance(queue_limit, int) or workers < 1 or queue_limit < 1:
        raise ValueError("检测并发和队列上限必须为正整数")

    detector = Detector(settings)
    items = list(candidates)
    results = []
    for start in range(0, len(items), queue_limit):
        batch = items[start:start + queue_limit]
        submitted_at = time.monotonic()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(detector.check_one, item, submitted_at) for item in batch]
            for index, future in enumerate(futures):
                try:
                    results.append(future.result())
                except Exception as exc:
                    results.append({
                        **batch[index],
                        "checks": {
                            key: {"status": INCOMPLETE, "reason": f"检测程序异常：{exc}"}
                            for key in ("ipv4", "http", "hls", "ts")
                        },
                    })
    return results
