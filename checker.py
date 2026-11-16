"""IPTV-Direct 本地 IPv4、HTTP、HLS 与 MPEG-TS 检测。"""

from __future__ import annotations

import http.client
import queue
import re
import socket
import ssl
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlsplit

PASS, FAIL, INCOMPLETE = "PASS", "FAIL", "INCOMPLETE"
NA = "N/A"

HLS_MARKERS = (
    "#EXT-X-STREAM-INF:",
    "#EXT-X-TARGETDURATION:",
    "#EXT-X-MEDIA-SEQUENCE:",
    "#EXT-X-PLAYLIST-TYPE:",
    "#EXT-X-I-FRAMES-ONLY",
    "#EXT-X-ENDLIST",
)


class CheckError(Exception):
    def __init__(
        self,
        status,
        reason,
        connected=False,
        ts_status=INCOMPLETE,
        ts_reason="TS 分片尚未完成实际检测",
    ):
        super().__init__(reason)
        self.status = status
        self.connected = connected
        self.ts_status = ts_status
        self.ts_reason = ts_reason


class DeadlineResolver:
    """使用有界后台线程执行 IPv4 DNS 解析。"""

    def __init__(self, workers):
        self.jobs = queue.Queue(maxsize=workers)
        self.workers = workers
        self.started = False
        self.lock = threading.Lock()

    def _worker(self):
        while True:
            host, port, event, result = self.jobs.get()
            try:
                result["addresses"] = socket.getaddrinfo(
                    host, port, socket.AF_INET, socket.SOCK_STREAM
                )
            except Exception as exc:
                result["error"] = exc
            finally:
                event.set()
                self.jobs.task_done()

    def _start(self):
        with self.lock:
            if self.started:
                return
            for index in range(self.workers):
                threading.Thread(
                    target=self._worker,
                    name=f"iptv-dns-{index}",
                    daemon=True,
                ).start()
            self.started = True

    def resolve(self, host, port, deadline):
        self._start()
        event, result = threading.Event(), {}
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise socket.timeout("DNS 解析超过检测时间预算")
        try:
            self.jobs.put((host, port, event, result), timeout=remaining)
        except queue.Full as exc:
            raise socket.timeout("DNS 等待队列已满") from exc

        remaining = deadline - time.monotonic()
        if remaining <= 0 or not event.wait(remaining):
            raise socket.timeout("DNS 解析超过检测时间预算")
        if "error" in result:
            raise result["error"]
        return result["addresses"]


class IPv4HTTPConnection(http.client.HTTPConnection):
    def __init__(self, host, port=None, *, deadline, resolver, **kwargs):
        super().__init__(host, port, **kwargs)
        self.deadline = deadline
        self.resolver = resolver
        self.ipv4_connected = False

    def connect(self):
        addresses = self.resolver.resolve(
            self.host, self.port, self.deadline
        )
        last_error = None
        for family, socktype, proto, _, address in addresses:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise socket.timeout("IPv4 连接超过检测时间预算")
            sock = socket.socket(family, socktype, proto)
            try:
                sock.settimeout(remaining)
                sock.connect(address)
                self.sock = sock
                self.ipv4_connected = True
                return
            except OSError as exc:
                last_error = exc
                sock.close()
        raise last_error or OSError("没有可用的 IPv4 地址")


class IPv4HTTPSConnection(IPv4HTTPConnection):
    def __init__(self, host, port=None, *, context=None, **kwargs):
        super().__init__(host, port, **kwargs)
        self.context = context or ssl.create_default_context()

    def connect(self):
        super().connect()
        try:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise socket.timeout("TLS 握手超过检测时间预算")
            self.sock.settimeout(remaining)
            self.sock = self.context.wrap_socket(
                self.sock, server_hostname=self.host
            )
        except Exception:
            if self.sock:
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
        self.resolver = DeadlineResolver(settings["local_concurrency"])

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
        match = re.fullmatch(
            r"bytes ([0-9]+)-([0-9]+)/([0-9]+|\*)",
            value or "",
            re.IGNORECASE,
        )
        if not match:
            raise CheckError(
                FAIL, "HTTP 206 的 Content-Range 无效", connected
            )

        start, end = int(match.group(1)), int(match.group(2))
        total = None if match.group(3) == "*" else int(match.group(3))
        if start != 0 or end < start or end >= limit or (
            total is not None and end >= total
        ):
            raise CheckError(
                FAIL, "HTTP 206 的范围或总长度无效", connected
            )
        return start, end, total

    @staticmethod
    def _abort(conn):
        """shutdown 能中断其他线程中的阻塞读取；单纯 close 不可靠。"""
        sock = getattr(conn, "sock", None)
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        try:
            conn.close()
        except OSError:
            pass

    @staticmethod
    def _read(response, conn, limit, deadline, connected):
        chunks, size = [], 0
        while size < limit:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CheckError(
                    INCOMPLETE,
                    "超过单候选主动检测时间预算",
                    connected,
                )
            sock = getattr(conn, "sock", None)
            try:
                if sock is not None:
                    sock.settimeout(remaining)
                part = response.read1(min(8192, limit - size))
            except (socket.timeout, TimeoutError, OSError) as exc:
                raise CheckError(
                    INCOMPLETE,
                    f"响应读取超时或连接中断：{exc}",
                    connected,
                ) from exc
            if not part:
                break
            chunks.append(part)
            size += len(part)
        return b"".join(chunks)

    def _request(
        self,
        url,
        deadline,
        queue_deadline,
        limit,
        use_range=True,
        first_slot=None,
        quality=None,
        stage="request",
    ):
        """执行一次请求，并记录不含 URL/主机信息的质量数据。"""
        started = time.monotonic()
        metrics = {}
        result = None
        error = None
        try:
            result = self._request_impl(
                url,
                deadline,
                queue_deadline,
                limit,
                use_range=use_range,
                first_slot=first_slot,
                metrics=metrics,
            )
            return result
        except Exception as exc:
            error = exc
            raise
        finally:
            finished = time.monotonic()
            elapsed = max(0.0, finished - started)
            read_seconds = max(0.0, metrics.get("read_seconds", 0.0))
            data = result.get("data", b"") if result else b""
            bytes_read = len(data) if result else metrics.get("bytes_read", 0)
            outcome = PASS if result is not None else getattr(
                error, "status", FAIL
            )
            if outcome not in (PASS, FAIL, INCOMPLETE):
                outcome = FAIL

            reason = str(error) if error is not None else ""

            # 除了识别异常文字，也检查请求是否已经接近候选检测期限。
            # 这样即使底层 socket 被期限定时器中断，异常文字没有出现
            # “timeout/超时”，也不会漏记超时质量数据。
            deadline_exhausted = finished >= deadline - 0.1
            is_timeout = (
                outcome == INCOMPLETE
                and (
                    isinstance(error, (socket.timeout, TimeoutError))
                    or "超时" in reason
                    or "超过" in reason
                    or "timeout" in reason.lower()
                    or deadline_exhausted
                )
            )

            sample = {
                "stage": stage,
                "status_code": (
                    result.get("status")
                    if result is not None
                    else metrics.get("status_code")
                ),
                "elapsed_seconds": round(elapsed, 6),
                "read_seconds": round(read_seconds, 6),
                "bytes_read": bytes_read,
                "bytes_per_second": (
                    round(bytes_read / read_seconds, 2)
                    if read_seconds > 0
                    else None
                ),
                "outcome": outcome,
                "redirect_count": metrics.get("redirect_count", 0),
            }
            if quality is not None:
                quality["request_count"] += 1
                quality["total_elapsed_seconds"] += elapsed
                quality["total_read_seconds"] += read_seconds
                quality["total_bytes_read"] += bytes_read
                if result is not None:
                    quality["request_success_count"] += 1
                else:
                    quality["request_failure_count"] += 1
                if is_timeout:
                    quality["timeout_count"] += 1
                quality["request_samples"].append(sample)

    def _request_impl(
        self,
        url,
        deadline,
        queue_deadline,
        limit,
        use_range=True,
        first_slot=None,
        metrics=None,
    ):
        if metrics is None:
            metrics = {}
        current, redirects = url, 0
        held_slot = first_slot

        while True:
            conn = None
            timer = None
            slot = held_slot
            acquired = slot is not None
            held_slot = None
            connected = False

            metrics["redirect_count"] = redirects
            try:
                parsed = urlsplit(current)
                scheme = parsed.scheme.lower()
                if (
                    scheme not in ("http", "https")
                    or not parsed.hostname
                    or parsed.username is not None
                    or parsed.password is not None
                ):
                    raise CheckError(
                        FAIL, "只支持无凭据的 HTTP/HTTPS URL"
                    )

                host = parsed.hostname
                if slot is None:
                    slot = self._slot(host)
                    wait = min(queue_deadline, deadline) - time.monotonic()
                    if wait <= 0 or not slot.acquire(timeout=wait):
                        raise CheckError(
                            INCOMPLETE,
                            "等待主机并发槽位超过剩余时间预算",
                        )
                    acquired = True

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CheckError(
                        INCOMPLETE, "超过单候选主动检测时间预算"
                    )

                port = parsed.port or (443 if scheme == "https" else 80)
                cls = (
                    IPv4HTTPSConnection
                    if scheme == "https"
                    else IPv4HTTPConnection
                )
                options = {
                    "timeout": remaining,
                    "deadline": deadline,
                    "resolver": self.resolver,
                }
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

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CheckError(
                        INCOMPLETE, "超过单候选主动检测时间预算"
                    )
                timer = threading.Timer(
                    remaining, self._abort, args=(conn,)
                )
                timer.daemon = True
                timer.start()

                conn.request("GET", path, headers=headers)
                response = conn.getresponse()
                metrics["status_code"] = response.status
                connected = bool(conn.ipv4_connected)

                if response.status in (301, 302, 303, 307, 308):
                    location = response.getheader("Location")
                    if not location:
                        raise CheckError(
                            FAIL, "重定向缺少 Location", connected
                        )
                    if redirects >= self.max_redirects:
                        raise CheckError(
                            FAIL, "超过最大重定向次数", connected
                        )
                    current = urljoin(current, location)
                    redirects += 1
                    continue

                if response.status not in (200, 206):
                    status = FAIL if response.status < 500 else INCOMPLETE
                    raise CheckError(
                        status,
                        f"HTTP 状态码 {response.status}",
                        connected,
                    )

                content_length = self._content_length(
                    response.getheader("Content-Length"), connected
                )
                range_info = None
                if response.status == 206:
                    range_info = self._content_range(
                        response.getheader("Content-Range", ""),
                        connected,
                        limit,
                    )
                    expected = range_info[1] - range_info[0] + 1
                    if (
                        content_length is not None
                        and content_length != expected
                    ):
                        raise CheckError(
                            FAIL,
                            "HTTP 206 的 Content-Length 与范围不一致",
                            connected,
                        )
                else:
                    expected = (
                        content_length
                        if content_length is not None
                        and content_length <= limit
                        else None
                    )

                read_started = time.monotonic()
                try:
                    data = self._read(
                        response, conn, limit, deadline, connected
                    )
                finally:
                    metrics["read_seconds"] = max(
                        0.0, time.monotonic() - read_started
                    )
                    metrics["bytes_read"] = (
                        len(data) if "data" in locals() else 0
                    )
                    metrics["redirect_count"] = redirects
                if not data:
                    raise CheckError(
                        FAIL, "HTTP 响应没有实际内容", connected
                    )
                if expected is not None and len(data) != expected:
                    raise CheckError(
                        INCOMPLETE,
                        "HTTP 响应正文长度与声明不一致",
                        connected,
                    )
                if (
                    response.status == 200
                    and content_length is not None
                    and content_length > limit
                    and len(data) < limit
                ):
                    raise CheckError(
                        INCOMPLETE,
                        "HTTP 正文未达到读取上限且提前结束",
                        connected,
                    )

                if response.status == 206:
                    total = range_info[2]
                    complete = (
                        total is not None
                        and range_info[1] + 1 == total
                        and len(data) == total
                    )
                elif content_length is not None:
                    complete = content_length == len(data)
                else:
                    complete = len(data) < limit

                return {
                    "data": data,
                    "url": current,
                    "status": response.status,
                    "content_type": response.getheader(
                        "Content-Type", ""
                    ),
                    "content_length": content_length,
                    "content_range": response.getheader(
                        "Content-Range", ""
                    ),
                    "range_total": (
                        range_info[2] if range_info else None
                    ),
                    "complete": complete,
                    "connected": connected,
                    "read_seconds": metrics.get("read_seconds", 0.0),
                    "redirect_count": redirects,
                }

            except CheckError as exc:
                if not exc.connected and conn is not None:
                    exc.connected = bool(
                        getattr(conn, "ipv4_connected", False)
                    )
                raise
            except (socket.timeout, TimeoutError) as exc:
                connected = bool(
                    getattr(conn, "ipv4_connected", False)
                )
                raise CheckError(
                    INCOMPLETE,
                    f"连接或读取超时：{exc}",
                    connected,
                ) from exc
            except (
                OSError,
                ssl.SSLError,
                http.client.HTTPException,
                ValueError,
            ) as exc:
                connected = bool(
                    getattr(conn, "ipv4_connected", False)
                )
                status = (
                    INCOMPLETE
                    if time.monotonic() >= deadline
                    else FAIL
                )
                raise CheckError(
                    status,
                    f"IPv4/HTTP 请求失败：{exc}",
                    connected,
                ) from exc
            finally:
                if timer:
                    timer.cancel()
                if conn:
                    conn.close()
                if acquired:
                    slot.release()

    @staticmethod
    def _ts_ok(data):
        if len(data) < 188 * 4 + 1:
            return False
        return any(
            all(
                data[offset + 188 * index] == 0x47
                for index in range(5)
            )
            for offset in range(min(188, len(data) - 188 * 4))
        )

    @staticmethod
    def _mark_hls_type(data, candidate_result):
        """从已读取的实际内容识别 HLS 结构，避免超限时类型仍为 UNKNOWN。"""
        if candidate_result is None:
            return False

        probe = data.lstrip()
        if probe.startswith(b"\xef\xbb\xbf"):
            probe = probe[3:].lstrip()

        try:
            lines = [
                line.strip()
                for line in probe.decode(
                    "utf-8-sig", errors="ignore"
                ).splitlines()
                if line.strip()
            ]
        except (UnicodeDecodeError, AttributeError):
            return False

        if not lines or lines[0] != "#EXTM3U":
            return False

        if not any(line.startswith(HLS_MARKERS) for line in lines):
            return False

        candidate_result["media_type"] = "HLS"
        return True

    @staticmethod
    def _playlist(data, candidate_result=None):
        """先识别 HLS 结构，再判断是否属于首期支持范围。"""
        try:
            lines = [
                line.strip()
                for line in data.decode("utf-8-sig").splitlines()
                if line.strip()
            ]
        except UnicodeDecodeError as exc:
            raise CheckError(
                FAIL, "HLS 清单不是有效 UTF-8"
            ) from exc

        if not lines or lines[0] != "#EXTM3U":
            raise CheckError(FAIL, "不是有效的 HLS 播放清单")

        if not any(line.startswith(HLS_MARKERS) for line in lines):
            raise CheckError(
                FAIL, "这是频道订阅 M3U，不是 HLS 播放清单"
            )

        if candidate_result is not None:
            candidate_result["media_type"] = "HLS"

        for line in lines:
            if line.startswith(
                ("#EXT-X-KEY:", "#EXT-X-SESSION-KEY:")
            ):
                match = re.search(
                    r"(?:^|[:,])METHOD=([^,]+)",
                    line.upper(),
                )
                if (
                    not match
                    or match.group(1).strip() != "NONE"
                ):
                    raise CheckError(FAIL, "不支持加密 HLS")

        if any(
            line.startswith("#EXT-X-MAP:")
            for line in lines
        ):
            raise CheckError(FAIL, "不支持 fMP4 HLS")

        if any(
            line.startswith("#EXT-X-BYTERANGE:")
            for line in lines
        ):
            raise CheckError(
                INCOMPLETE, "不支持 HLS 分片字节范围"
            )

        if any(
            line.startswith("#EXT-X-PART:")
            for line in lines
        ):
            raise CheckError(
                FAIL, "不支持低延迟 fMP4 分片"
            )

        return lines

    def _complete_manifest(self, result, label):
        length = result["content_length"]
        total = result["range_total"]
        if (
            length is not None
            and length > self.manifest_limit
        ) or (
            total is not None
            and total > self.manifest_limit
        ) or len(result["data"]) > self.manifest_limit:
            raise CheckError(FAIL, f"{label}超过 1 MB")
        if not result["complete"]:
            raise CheckError(
                INCOMPLETE, f"{label}不完整，无法确认全部内容"
            )
        return result["data"]

    def _manifest(
        self, deadline, queue_deadline, first, candidate_result,
        quality=None,
    ):
        # 关键修正：在完整清单读取和大小检查之前，
        # 先根据首次读取到的实际内容确认是否具备 HLS 结构。
        # 普通频道订阅 M3U 只有 #EXTM3U / #EXTINF，
        # 不含 HLS_MARKERS，因此不会被误识别为 HLS。
        self._mark_hls_type(first["data"], candidate_result)

        result = first
        if not first["complete"]:
            result = self._request(
                first["url"],
                deadline,
                queue_deadline,
                self.manifest_limit,
                use_range=True,
                quality=quality,
                stage="main_playlist",
            )

        data = self._complete_manifest(result, "HLS 清单")
        final_url = result["url"]
        lines = self._playlist(data, candidate_result)

        if any(
            line.startswith("#EXT-X-STREAM-INF:")
            for line in lines
        ):
            variants = [
                urljoin(final_url, lines[index + 1])
                for index, line in enumerate(lines[:-1])
                if line.startswith("#EXT-X-STREAM-INF:")
                and not lines[index + 1].startswith("#")
            ]
            if not variants:
                raise CheckError(
                    FAIL, "主播放清单没有媒体播放列表"
                )
            media = self._request(
                variants[0],
                deadline,
                queue_deadline,
                self.manifest_limit,
                use_range=True,
                quality=quality,
                stage="media_playlist",
            )
            media_data = self._complete_manifest(
                media, "媒体播放清单"
            )
            media_url = media["url"]
            media_lines = self._playlist(
                media_data, candidate_result
            )
        else:
            media_url, media_lines = final_url, lines

        segments, has_extinf = [], False
        for line in media_lines:
            if line.startswith("#EXTINF:"):
                has_extinf = True
            elif not line.startswith("#"):
                if not has_extinf:
                    raise CheckError(
                        FAIL, "媒体分片缺少对应的 EXTINF"
                    )
                segments.append(urljoin(media_url, line))
                has_extinf = False

        if has_extinf:
            raise CheckError(
                INCOMPLETE,
                "媒体清单最后一个 EXTINF 缺少分片地址",
            )
        if len(segments) < 2:
            raise CheckError(
                INCOMPLETE, "媒体清单不足两个分片"
            )

        for index, segment in enumerate(segments[:3], start=1):
            if quality is not None:
                quality["segment_total"] += 1
            try:
                part = self._request(
                    segment,
                    deadline,
                    queue_deadline,
                    self.ts_limit,
                    use_range=True,
                    quality=quality,
                    stage=f"segment_{index}",
                )
                if not self._ts_ok(part["data"]):
                    raise CheckError(
                        FAIL,
                        "HLS 分片不是有效 MPEG-TS 数据",
                        ts_status=FAIL,
                        ts_reason="已读取实际 HLS 分片，但 TS 同步结构无效",
                    )
                if quality is not None:
                    quality["segment_success_count"] += 1
            except CheckError:
                if quality is not None:
                    quality["segment_failure_count"] += 1
                raise

        return {
            "status": PASS,
            "reason": (
                f"媒体清单及 {min(3, len(segments))} 个 TS 分片通过"
            ),
        }

    def check_one(self, candidate, submitted_at):
        result = {
            **candidate,
            "media_type": "UNKNOWN",
            "quality": {
                "request_count": 0,
                "request_success_count": 0,
                "request_failure_count": 0,
                "timeout_count": 0,
                "total_bytes_read": 0,
                "total_elapsed_seconds": 0.0,
                "total_read_seconds": 0.0,
                "segment_total": 0,
                "segment_success_count": 0,
                "segment_failure_count": 0,
                "request_samples": [],
            },
            "checks": {
                key: {"status": INCOMPLETE, "reason": "尚未检测"}
                for key in ("ipv4", "http", "hls", "ts")
            },
        }
        url = candidate.get("normalized_url")
        if not url:
            for item in result["checks"].values():
                item.update(
                    status=FAIL, reason="缺少规范化 URL"
                )
            return result

        slot = None
        slot_acquired = False
        try:
            parsed = urlsplit(url)
            host = parsed.hostname
            if (
                not host
                or parsed.scheme.lower() not in ("http", "https")
            ):
                raise ValueError(
                    "URL 缺少有效主机名或协议"
                )
            if (
                parsed.username is not None
                or parsed.password is not None
            ):
                raise ValueError(
                    "URL 不允许包含用户名或密码"
                )
            _ = parsed.port

            slot = self._slot(host)
            queue_deadline = (
                submitted_at + self.settings["max_queue_seconds"]
            )
            wait = queue_deadline - time.monotonic()
            if wait <= 0 or not slot.acquire(timeout=wait):
                raise CheckError(
                    INCOMPLETE,
                    f"等待主机并发槽位超过 {self.settings['max_queue_seconds']} 秒",
                )
            slot_acquired = True

            deadline = time.monotonic() + self.budget
            first_slot, slot = slot, None
            # 信号量所有权已移交给 _request，由 _request 负责释放。
            slot_acquired = False
            first = self._request(
                url,
                deadline,
                queue_deadline,
                self.ts_limit,
                use_range=True,
                first_slot=first_slot,
                quality=result["quality"],
                stage="initial_response",
            )
            result["checks"]["ipv4"] = {
                "status": PASS,
                "reason": "已建立最终目标的真实 IPv4 连接",
            }
            result["checks"]["http"] = {
                "status": PASS,
                "reason": (
                    f"HTTP {first['status']} 且读取到实际内容"
                ),
            }

            probe = first["data"].lstrip()
            if probe.startswith(b"\xef\xbb\xbf"):
                probe = probe[3:].lstrip()

            if probe.startswith(b"#EXTM3U"):
                try:
                    if self._mark_hls_type(first["data"], result):
                        if result["quality"]["request_samples"]:
                            result["quality"]["request_samples"][-1]["stage"] = (
                                "main_playlist"
                            )
                    hls = self._manifest(
                        deadline,
                        queue_deadline,
                        first,
                        result,
                        quality=result["quality"],
                    )
                    result["checks"]["hls"] = hls
                    result["checks"]["ts"] = {
                        "status": PASS,
                        "reason": hls["reason"],
                    }
                except CheckError as exc:
                    result["checks"]["hls"] = {
                        "status": exc.status,
                        "reason": str(exc),
                    }
                    result["checks"]["ts"] = {
                        "status": exc.ts_status,
                        "reason": exc.ts_reason,
                    }

            elif self._ts_ok(first["data"]):
                if result["quality"]["request_samples"]:
                    result["quality"]["request_samples"][-1]["stage"] = (
                        "direct_ts"
                    )
                result["media_type"] = "DIRECT_TS"
                result["checks"]["ts"] = {
                    "status": PASS,
                    "reason": (
                        "Range 数据通过 5 个 TS 同步位置检查"
                    ),
                }
                result["checks"]["hls"] = {
                    "status": NA,
                    "reason": (
                        "已识别为直接 TS，HLS 检测不适用"
                    ),
                }

            else:
                result["checks"]["hls"] = {
                    "status": FAIL,
                    "reason": "响应不是受支持的 HLS 清单",
                }
                result["checks"]["ts"] = {
                    "status": FAIL,
                    "reason": (
                        "响应不符合 MPEG-TS 同步结构"
                    ),
                }

        except CheckError as exc:
            result["checks"]["ipv4"] = {
                "status": PASS if exc.connected else exc.status,
                "reason": (
                    "已建立真实 IPv4 连接"
                    if exc.connected
                    else str(exc)
                ),
            }
            result["checks"]["http"] = {
                "status": exc.status,
                "reason": str(exc),
            }
            result["checks"]["hls"] = {
                "status": INCOMPLETE,
                "reason": "前置 HTTP 检测未通过",
            }
            result["checks"]["ts"] = {
                "status": INCOMPLETE,
                "reason": "未取得可检查的媒体数据",
            }

        except (ValueError, TypeError) as exc:
            for item in result["checks"].values():
                item.update(
                    status=FAIL,
                    reason=f"候选 URL 无效：{exc}",
                )

        finally:
            # 只有当前函数确实成功获取且仍持有槽位时才能释放。
            # acquire 超时失败时，slot 变量虽然非空，但不拥有该信号量。
            if slot is not None and slot_acquired:
                slot.release()

        return result


def local_checks_passed(candidate):
    """判断本地适用的检测项目是否全部通过。"""
    if not isinstance(candidate, dict):
        return False

    checks = candidate.get("checks")
    if not isinstance(checks, dict):
        return False

    media_type = candidate.get("media_type")
    required = ("ipv4", "http", "ts")

    if media_type == "HLS":
        required += ("hls",)
    elif media_type == "DIRECT_TS":
        hls_check = checks.get("hls")
        if not isinstance(hls_check, dict) or hls_check.get("status") != NA:
            return False
    else:
        return False

    return all(
        isinstance(checks.get(name), dict)
        and checks[name].get("status") == PASS
        for name in required
    )


def check_candidates(candidates, settings):
    """按输入顺序检测候选；settings 接收 settings.json 的 detection 区段。"""
    required = (
        "local_concurrency",
        "per_host_concurrency",
        "queue_limit",
        "max_queue_seconds",
        "candidate_active_budget_seconds",
        "max_hls_manifest_bytes",
        "max_ts_read_bytes",
        "max_redirects",
    )
    if (
        not isinstance(settings, dict)
        or any(key not in settings for key in required)
    ):
        raise ValueError(
            "checker 缺少必要的 detection 配置"
        )

    for key in required:
        value = settings[key]
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < 1
        ):
            raise ValueError(
                f"检测配置 {key} 必须为正整数"
            )

    detector = Detector(settings)
    items, results = list(candidates), []
    workers = settings["local_concurrency"]
    queue_limit = settings["queue_limit"]

    for start in range(0, len(items), queue_limit):
        batch = items[start:start + queue_limit]
        submitted_at = time.monotonic()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(
                    detector.check_one,
                    item,
                    submitted_at,
                )
                for item in batch
            ]
            for index, future in enumerate(futures):
                try:
                    results.append(future.result())
                except Exception as exc:
                    results.append({
                        **batch[index],
                        "media_type": "UNKNOWN",
                        "quality": {
                            "request_count": 0,
                            "request_success_count": 0,
                            "request_failure_count": 0,
                            "timeout_count": 0,
                            "total_bytes_read": 0,
                            "total_elapsed_seconds": 0.0,
                            "total_read_seconds": 0.0,
                            "segment_total": 0,
                            "segment_success_count": 0,
                            "segment_failure_count": 0,
                            "request_samples": [],
                        },
                        "checks": {
                            key: {
                                "status": INCOMPLETE,
                                "reason": f"检测程序异常：{exc}",
                            }
                            for key in (
                                "ipv4", "http", "hls", "ts"
                            )
                        },
                    })

    return results
