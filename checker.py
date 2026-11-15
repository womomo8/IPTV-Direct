
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


class CheckError(Exception):
    def __init__(self, status, reason, connected=False):
        super().__init__(reason)
        self.status = status
        self.connected = connected


class DeadlineResolver:
    """使用有界后台线程执行 DNS，避免 DNS 等待无限占用检测线程。"""

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
                thread = threading.Thread(
                    target=self._worker,
                    name=f"iptv-dns-{index}",
                    daemon=True,
                )
                thread.start()
            self.started = True

    def resolve(self, host, port, deadline):
        self._start()
        event = threading.Event()
        result = {}
        remaining = deadline - time.monotonic()

        if remaining <= 0:
            raise socket.timeout("DNS 解析超过检测时间预算")

        try:
            self.jobs.put(
                (host, port, event, result),
                timeout=remaining,
            )
        except queue.Full:
            raise socket.timeout("DNS 等待队列已满")

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
        error = None

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
            r"bytes ([0-9]+)-([0-9]+)/([0-9]+|\\*)",
            value or "",
            re.IGNORECASE,
        )
        if not match:
            raise CheckError(FAIL, "HTTP 206 的 Content-Range 无效", connected)

        start, end = int(match.group(1)), int(match.group(2))
        total = None if match.group(3) == "*" else int(match.group(3))

        if (
            start != 0
            or end < start
            or end >= limit
            or (total is not None and end >= total)
        ):
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
            except (socket.timeout, TimeoutError, OSError) as exc:
                raise CheckError(
                    INCOMPLETE, f"响应读取超时或连接中断：{exc}", connected
                )

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
    ):
        current = url
        redirects = 0
        held_slot = first_slot
        connected_any = False

        while True:
            conn = None
            timer = None
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
                    raise CheckError(
                        FAIL, "只支持无凭据的 HTTP/HTTPS URL", connected_any
                    )

                host = parsed.hostname

                if slot is None:
                    slot = self._slot(host)
                    wait_until = min(queue_deadline, deadline)
                    wait = wait_until - time.monotonic()

                    if wait <= 0 or not slot.acquire(timeout=wait):
                        raise CheckError(
                            INCOMPLETE,
                            "等待主机并发槽位超过剩余时间预算",
                            connected_any,
                        )
                    acquired = True

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CheckError(
                        INCOMPLETE, "超过单候选主动检测时间预算", connected_any
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
                        INCOMPLETE, "超过单候选主动检测时间预算", connected_any
                    )

                timer = threading.Timer(remaining, conn.close)
                timer.daemon = True
                timer.start()

                conn.request("GET", path, headers=headers)
                response = conn.getresponse()

                connected = bool(getattr(conn, "ipv4_connected", False))
                connected_any = connected_any or connected

                if respon
