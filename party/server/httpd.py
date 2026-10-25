"""HTTP + SSE transport on ``http.server.ThreadingHTTPServer``.

Security posture
----------------
* Credentials only via ``Authorization: Bearer``. Any query string on an API
  route is rejected before authentication, so a token placed in a URL is
  never accepted.
* ``Host`` must be one of the server's own origins (DNS-rebinding guard).
  ``Origin``, when present, must exactly equal an allowed origin.
  ``Sec-Fetch-Site``, when present, must be ``same-origin`` or ``none``. No
  CORS headers are ever emitted.
* Built-in request logging is disabled. The optional access log records only
  method, route template and status: no paths, query strings or headers.
  Unexpected exceptions are logged by class name only, never tracebacks.
* Static files (``--web-dir`` only) come from the exact allowlist in
  ``static.py``. The only accepted query string anywhere is
  ``/play.html?code=<room code>``; every other query, including any on an API
  route, is rejected. HTML gets a CSP allowing same-origin modules/CSS only.
"""

import ipaddress
import json
import re
import select
import socket
import socketserver
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from . import PROTOCOL, static
from .errors import PartyError, error_body
from .manager import RateLimiter, RoomManager
from .modes import catalog

ROOM_ROUTE = re.compile(r"/api/rooms/([A-Za-z0-9]{1,16})/(join|state|events|actions)")  # fullmatch only
API_CSP = "default-src 'none'; frame-ancestors 'none'"
SSE_RETRY_MS = 3000
MAX_DRAIN_BYTES = 1024 * 1024

_SEND_ERROR_CODES = {
    400: "bad_request", 404: "not_found", 405: "method_not_allowed", 408: "bad_request",
    413: "body_too_large", 414: "bad_request", 431: "bad_request", 501: "method_not_allowed",
    505: "bad_request",
}


def _dumps(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def is_loopback_host(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def normalize_origin(value):
    """``scheme://host[:port]`` lowercased, or raise ValueError."""
    parts = urlsplit(value.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError("origin must be http(s)://host[:port]")
    if parts.path not in ("", "/") or parts.query or parts.fragment or "@" in parts.netloc:
        raise ValueError("origin must not contain a path, query, fragment or userinfo")
    return "%s://%s" % (parts.scheme, parts.netloc.lower())


class PartyApp(object):
    def __init__(self, config, contents, log_stream=None, web=None):
        self.config = config
        self.contents = contents
        self.web = web  # static.StaticRoot or None (API only)
        self.manager = RoomManager(config, contents)
        self.request_limiter = RateLimiter(config.request_rate, config.request_burst)
        self.create_limiter = RateLimiter(config.create_per_minute / 60.0, config.create_burst)
        self.join_limiter = RateLimiter(config.join_per_minute / 60.0, config.join_burst)
        self.action_limiter = RateLimiter(config.action_rate, config.action_burst)
        self.connection_slots = threading.BoundedSemaphore(int(config.max_connections))
        self.allowed_origins = frozenset()
        self.allowed_hosts = frozenset()
        self._log_stream = log_stream if log_stream is not None else sys.stderr
        self._log_lock = threading.Lock()

    def set_allowed_origins(self, origins):
        origins = frozenset(normalize_origin(o) for o in origins)
        self.allowed_origins = origins
        self.allowed_hosts = frozenset(urlsplit(o).netloc for o in origins)

    def log(self, line):
        with self._log_lock:
            try:
                self._log_stream.write(line + "\n")
                self._log_stream.flush()
            except (OSError, ValueError):
                pass

    def access(self, method, route, status):
        if self.config.access_log:
            self.log("access %s %s %s" % (method or "-", route, status or "-"))


class PartyHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64

    def __init__(self, address, app):
        self.app = app
        ThreadingHTTPServer.__init__(self, address, PartyRequestHandler)

    def server_bind(self):
        # Skip HTTPServer.server_bind's reverse-DNS getfqdn() lookup.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port

    def process_request(self, request, client_address):
        if not self.app.connection_slots.acquire(blocking=False):
            body = _dumps({"ok": False, "error": error_body("server_busy")}).encode("utf-8")
            head = ("HTTP/1.0 503 Service Unavailable\r\nContent-Type: application/json; charset=utf-8\r\n"
                    "Content-Length: %d\r\nConnection: close\r\n\r\n" % len(body)).encode("ascii")
            try:
                request.settimeout(1.0)
                request.sendall(head + body)
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            ThreadingHTTPServer.process_request(self, request, client_address)
        except Exception:
            self.app.connection_slots.release()
            self.shutdown_request(request)

    def process_request_thread(self, request, client_address):
        try:
            ThreadingHTTPServer.process_request_thread(self, request, client_address)
        finally:
            self.app.connection_slots.release()

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        self.app.log("connection_error %s" % type(exc).__name__)

    def server_close(self):
        ThreadingHTTPServer.server_close(self)
        if self.app.web is not None:
            self.app.web.close()


class PartyRequestHandler(BaseHTTPRequestHandler):
    server_version = "MajlisParty/1"
    sys_version = ""
    protocol_version = "HTTP/1.0"

    def setup(self):
        self.timeout = self.server.app.config.request_timeout
        self._route = "unmatched"
        self._status = None
        self._headers_sent = False
        self._body_consumed = False
        BaseHTTPRequestHandler.setup(self)

    # ----------------------------------------------------- logging overrides

    def version_string(self):
        return self.server_version

    def log_message(self, format, *args):
        # The default implementation prints the raw request line. Disabled.
        return

    def log_request(self, code="-", size="-"):
        self._status = code

    def send_error(self, code, message=None, explain=None):
        # Used by BaseHTTPRequestHandler for unparseable requests.
        self.close_connection = True
        try:
            self._send_json(code, {"ok": False, "error": error_body(_SEND_ERROR_CODES.get(code, "bad_request"))})
        except OSError:
            pass

    # -------------------------------------------------------------- methods

    def do_GET(self):
        self._dispatch()

    def do_POST(self):
        self._dispatch()

    def _method_not_allowed(self):
        self._dispatch(force_error="method_not_allowed")

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_HEAD = do_TRACE = do_CONNECT = _method_not_allowed

    # ----------------------------------------------------------- dispatching

    def _dispatch(self, force_error=None):
        app = self.server.app
        try:
            try:
                if force_error:
                    self._route = "method-rejected"
                    raise PartyError(force_error)
                self._handle(app)
            except PartyError as exc:
                if not self._headers_sent:
                    self._send_error_code(exc.code, exc.retry_after)
        except OSError:
            # Client went away or timed out; nothing useful to send.
            self.close_connection = True
        except Exception as exc:
            app.log("internal_error %s" % type(exc).__name__)
            if not self._headers_sent:
                try:
                    self._send_error_code("internal_error")
                except OSError:
                    pass
        finally:
            app.access(self.command, self._route, self._status)

    def _handle(self, app):
        raw = self.path
        if not raw.startswith("/") or raw.startswith("//"):
            raise PartyError("not_found")
        path, has_query, query = raw.partition("?")
        self._check_host_and_origin(app)
        static_file = static.lookup(path) if app.web is not None else None
        if has_query and not (static_file is not None and static.query_allowed(path, query)):
            self._route = "query-rejected"
            raise PartyError("query_not_allowed")
        client_ip = self.client_address[0]
        allowed, retry = app.request_limiter.allow(client_ip)
        if not allowed:
            raise PartyError("rate_limited", retry)
        method = self.command

        if static_file is not None:
            self._route = "static"
            self._require_method(method, "GET")
            parts, content_type = static_file
            data = app.web.read(parts)
            if data is None:
                raise PartyError("not_found")
            self._send_static(data, content_type)
            return
        if path == "/api/health":
            self._route = "/api/health"
            self._require_method(method, "GET")
            self._send_json(200, {"ok": True, "protocol": PROTOCOL})
            return
        if path == "/api/modes":
            self._route = "/api/modes"
            self._require_method(method, "GET")
            self._send_json(200, {"protocol": PROTOCOL, "modes": catalog(app.contents)})
            return
        if path == "/api/rooms":
            self._route = "/api/rooms"
            self._require_method(method, "POST")
            allowed, retry = app.create_limiter.allow(client_ip)
            if not allowed:
                raise PartyError("rate_limited", retry)
            body = self._read_json(app, allow_empty=True)
            self._send_json(201, app.manager.create_room(body))
            return
        match = ROOM_ROUTE.fullmatch(path)
        if not match:
            raise PartyError("not_found")
        code, leaf = match.group(1), match.group(2)
        self._route = "/api/rooms/:code/" + leaf
        manager = app.manager

        if leaf == "join":
            self._require_method(method, "POST")
            allowed, retry = app.join_limiter.allow(client_ip)
            if not allowed:
                raise PartyError("rate_limited", retry)
            body = self._read_json(app, allow_empty=True)
            token = self._bearer(required=False)
            room = manager.get_room(code)
            status, result = manager.join(room, body, token)
            self._send_json(status, result)
            return
        if leaf == "state":
            self._require_method(method, "GET")
            token = self._bearer()
            room = manager.get_room(code)
            principal = manager.authenticate(room, token)
            self._send_json(200, manager.snapshot(room, principal))
            return
        if leaf == "events":
            self._require_method(method, "GET")
            token = self._bearer()
            room = manager.get_room(code)
            principal = manager.authenticate(room, token)
            self._serve_events(app, room, principal)
            return
        # actions
        self._require_method(method, "POST")
        body = self._read_json(app, allow_empty=False)
        token = self._bearer()
        room = manager.get_room(code)
        principal = manager.authenticate(room, token)
        allowed, retry = app.action_limiter.allow(room.code + "/" + principal.key)
        if not allowed:
            raise PartyError("rate_limited", retry)
        status, result, replayed = manager.submit_action(room, principal, body)
        self._send_json(status, result, {"X-Action-Replayed": "1" if replayed else "0"})

    # ------------------------------------------------------------- checks

    @staticmethod
    def _require_method(method, expected):
        if method != expected:
            raise PartyError("method_not_allowed")

    def _check_host_and_origin(self, app):
        hosts = self.headers.get_all("Host") or []
        if len(hosts) != 1 or hosts[0].strip().lower() not in app.allowed_hosts:
            raise PartyError("host_forbidden")
        origins = self.headers.get_all("Origin") or []
        if len(origins) > 1 or (origins and origins[0] not in app.allowed_origins):
            raise PartyError("origin_forbidden")
        site = self.headers.get("Sec-Fetch-Site")
        if site is not None and site.strip().lower() not in ("same-origin", "none"):
            raise PartyError("origin_forbidden")

    def _bearer(self, required=True):
        values = self.headers.get_all("Authorization") or []
        if not values:
            if required:
                raise PartyError("unauthorized")
            return None
        if len(values) > 1:
            raise PartyError("unauthorized")
        scheme, _, token = values[0].strip().partition(" ")
        token = token.strip()
        if scheme.lower() != "bearer" or not token:
            raise PartyError("unauthorized")
        return token

    def _discard_unread_body(self):
        """Drain a small unread body so closing the socket does not reset the
        connection before the client reads the error response."""
        if self._body_consumed or self.headers is None:
            return
        self._body_consumed = True
        try:
            length = int((self.headers.get("Content-Length") or "0").strip())
        except ValueError:
            return
        if 0 < length <= MAX_DRAIN_BYTES:
            self._drain(length)

    def _read_json(self, app, allow_empty):
        self._body_consumed = True
        if self.headers.get("Transfer-Encoding") is not None:
            self.close_connection = True
            raise PartyError("length_required")
        length_header = self.headers.get("Content-Length")
        if length_header is None:
            if allow_empty:
                return {}
            raise PartyError("length_required")
        try:
            length = int(length_header.strip())
        except ValueError:
            raise PartyError("bad_request")
        if length < 0:
            raise PartyError("bad_request")
        if length > app.config.max_body_bytes:
            self.close_connection = True
            if length <= MAX_DRAIN_BYTES:
                self._drain(length)
            raise PartyError("body_too_large")
        if length == 0:
            if allow_empty:
                return {}
            raise PartyError("invalid_json")
        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if content_type != "application/json":
            self._drain(length)
            raise PartyError("unsupported_media_type")
        data = self.rfile.read(length)
        if len(data) != length:
            raise PartyError("bad_request")
        try:
            body = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise PartyError("invalid_json")
        if not isinstance(body, dict):
            raise PartyError("invalid_json")
        return body

    def _drain(self, length):
        remaining = length
        try:
            while remaining > 0:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
        except OSError:
            pass

    # ------------------------------------------------------------ responses

    def _common_headers(self, csp=API_CSP):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", csp)
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Connection", "close")

    def _send_static(self, data, content_type):
        self.send_response(200)
        self._common_headers(static.HTML_CSP if content_type == static.HTML else API_CSP)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self._headers_sent = True
        self.wfile.write(data)

    def _send_json(self, status, obj, extra_headers=None):
        data = _dumps(obj).encode("utf-8")
        self.send_response(status)
        self._common_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self._headers_sent = True
        if self.command != "HEAD":
            self.wfile.write(data)

    def _send_error_code(self, code, retry_after=None):
        if getattr(self, "headers", None) is not None:
            self._discard_unread_body()
        extra = {}
        if retry_after is not None:
            extra["Retry-After"] = str(max(1, int(retry_after + 0.999)))
        status = PartyError(code).status
        self._send_json(status, {"ok": False, "error": error_body(code)}, extra)

    # ------------------------------------------------------------------ SSE

    def _peer_closed(self):
        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
            if not readable:
                return False
            return self.connection.recv(1, socket.MSG_PEEK) == b""
        except (OSError, ValueError):
            return True

    def _serve_events(self, app, room, principal):
        manager = app.manager
        cfg = app.config
        # Last-Event-ID is advisory: the stream always starts with the current
        # projected snapshot, whatever (if anything) the client claims to have.
        manager.open_stream(room, principal)
        try:
            self.send_response(200)
            self._common_headers()
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self._headers_sent = True
            self.close_connection = True
            self.connection.settimeout(cfg.sse_write_timeout)
            self.wfile.write(("retry: %d\n\n" % SSE_RETRY_MS).encode("ascii"))
            last_revision = None
            heartbeat_at = manager.clock() + cfg.heartbeat_interval
            while True:
                event = manager.next_stream_event(room, principal, last_revision, heartbeat_at)
                kind = event[0]
                if kind == "close":
                    break
                if kind == "error":
                    frame = "event: error\ndata: %s\n\n" % _dumps({"ok": False, "error": error_body(event[1])})
                    self.wfile.write(frame.encode("utf-8"))
                    break
                if kind == "state":
                    last_revision = event[1]
                    frame = "id: %d\nevent: state\ndata: %s\n\n" % (event[1], _dumps(event[2]))
                else:
                    if self._peer_closed():
                        break
                    frame = ": heartbeat\n\n"
                self.wfile.write(frame.encode("utf-8"))
                heartbeat_at = manager.clock() + cfg.heartbeat_interval
        finally:
            manager.close_stream(room, principal)


def create_server(config, contents, host="127.0.0.1", port=0, extra_origins=(), allow_lan=False,
                  log_stream=None, web_dir=None):
    """Bind and return a ``PartyHTTPServer``. Loopback only unless ``allow_lan``.

    ``web_dir`` enables the static frontend adapter; None serves APIs only.
    """
    if host == "localhost":
        host = "127.0.0.1"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise ValueError("--host must be an IPv4 address or 'localhost'")
    if address.version != 4:
        raise ValueError("--host must be an IPv4 address or 'localhost'")
    if not allow_lan and not address.is_loopback:
        raise ValueError("refusing non-loopback bind without --allow-lan")
    origins = set(normalize_origin(o) for o in extra_origins)
    if address.is_unspecified and not origins:
        raise ValueError("binding 0.0.0.0 requires at least one --allowed-origin")
    web = static.StaticRoot(web_dir) if web_dir is not None else None
    app = PartyApp(config, contents, log_stream=log_stream, web=web)
    try:
        server = PartyHTTPServer((host, port), app)
    except BaseException:
        if web is not None:
            web.close()
        raise
    bound_host, bound_port = server.server_address[:2]
    if not address.is_unspecified:
        origins.add("http://%s:%d" % (bound_host, bound_port))
    if address.is_loopback:
        origins.add("http://127.0.0.1:%d" % bound_port)
        origins.add("http://localhost:%d" % bound_port)
    app.set_allowed_origins(origins)
    return server
