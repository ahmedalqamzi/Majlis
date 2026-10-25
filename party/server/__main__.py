"""CLI: ``python3 -m party.server [options]``.

Prints one JSON line to stdout once listening:
``{"event":"listening","host":...,"port":...,"url":...,"content":{...}}``.
No credentials or filesystem paths are ever printed. Binds 127.0.0.1 by
default; ``--port 0`` selects a free ephemeral port.
"""

import argparse
import json
import signal
import sys
import threading

from . import PROTOCOL
from .content import default_content_dir, load_all
from .httpd import create_server
from .manager import ServerConfig
from .modes import REGISTRY


def build_parser():
    d = ServerConfig.DEFAULTS
    p = argparse.ArgumentParser(prog="python3 -m party.server",
                                description="Majlis party suite backend (protocol v1, in-memory).")
    p.add_argument("--host", default="127.0.0.1", help="IPv4 bind address (default 127.0.0.1)")
    p.add_argument("--port", type=int, default=8765, help="TCP port; 0 picks a free port (default 8765)")
    p.add_argument("--allow-lan", action="store_true", help="permit a non-loopback bind address")
    p.add_argument("--allowed-origin", action="append", default=[], metavar="URL",
                   help="extra exact origin, e.g. http://192.168.1.20:8765 (repeatable)")
    p.add_argument("--content-dir", default=None, help="content directory (default party/content)")
    p.add_argument("--check-content", action="store_true", help="validate content, print report, exit")
    p.add_argument("--web-dir", default=None, metavar="DIR",
                   help="also serve the party/web frontend allowlist from DIR (default: APIs only)")
    p.add_argument("--min-players", type=int, default=d["min_players"])
    p.add_argument("--max-players", type=int, default=d["max_players"])
    p.add_argument("--room-idle-ttl", type=float, default=d["room_idle_ttl"])
    p.add_argument("--room-max-age", type=float, default=d["room_max_age"])
    p.add_argument("--token-ttl", type=float, default=d["token_ttl"])
    p.add_argument("--closed-room-grace", type=float, default=d["closed_room_grace"])
    p.add_argument("--max-rooms", type=int, default=d["max_rooms"])
    p.add_argument("--action-retention", type=int, default=d["action_retention"])
    p.add_argument("--heartbeat", type=float, default=d["heartbeat_interval"])
    p.add_argument("--sweep-interval", type=float, default=d["sweep_interval"])
    p.add_argument("--sse-write-timeout", type=float, default=d["sse_write_timeout"])
    p.add_argument("--request-timeout", type=float, default=d["request_timeout"])
    p.add_argument("--max-body-bytes", type=int, default=d["max_body_bytes"])
    p.add_argument("--max-streams-per-principal", type=int, default=d["max_streams_per_principal"])
    p.add_argument("--max-streams-per-room", type=int, default=d["max_streams_per_room"])
    p.add_argument("--max-streams-total", type=int, default=d["max_streams_total"])
    p.add_argument("--max-connections", type=int, default=d["max_connections"])
    p.add_argument("--request-rate", type=float, default=d["request_rate"])
    p.add_argument("--request-burst", type=int, default=d["request_burst"])
    p.add_argument("--create-per-minute", type=float, default=d["create_per_minute"])
    p.add_argument("--create-burst", type=int, default=d["create_burst"])
    p.add_argument("--join-per-minute", type=float, default=d["join_per_minute"])
    p.add_argument("--join-burst", type=int, default=d["join_burst"])
    p.add_argument("--action-rate", type=float, default=d["action_rate"])
    p.add_argument("--action-burst", type=int, default=d["action_burst"])
    p.add_argument("--access-log", action="store_true",
                   help="log 'method route-template status' lines to stderr (no paths, headers or tokens)")
    p.add_argument("--exit-on-stdin-eof", action="store_true",
                   help="shut down when stdin closes (used by tests so no server outlives its parent)")
    return p


def _config_from_args(args):
    return ServerConfig(
        min_players=args.min_players, max_players=args.max_players,
        room_idle_ttl=args.room_idle_ttl, room_max_age=args.room_max_age, token_ttl=args.token_ttl,
        closed_room_grace=args.closed_room_grace, max_rooms=args.max_rooms,
        action_retention=args.action_retention, heartbeat_interval=args.heartbeat,
        sweep_interval=args.sweep_interval, sse_write_timeout=args.sse_write_timeout,
        request_timeout=args.request_timeout, max_body_bytes=args.max_body_bytes,
        max_streams_per_principal=args.max_streams_per_principal,
        max_streams_per_room=args.max_streams_per_room, max_streams_total=args.max_streams_total,
        max_connections=args.max_connections, request_rate=args.request_rate,
        request_burst=args.request_burst, create_per_minute=args.create_per_minute,
        create_burst=args.create_burst, join_per_minute=args.join_per_minute,
        join_burst=args.join_burst, action_rate=args.action_rate, action_burst=args.action_burst,
        access_log=args.access_log,
    )


def _emit(obj, stream=None):
    stream = stream or sys.stdout
    try:
        stream.write(json.dumps(obj, sort_keys=True) + "\n")
        stream.flush()
    except (OSError, ValueError):
        pass


def main(argv=None):
    args = build_parser().parse_args(argv)
    content_dir = args.content_dir or default_content_dir()
    contents = load_all(content_dir, REGISTRY)

    if args.check_content:
        report = dict((mode, {"status": s.status, "issues": s.issues}) for mode, s in contents.items())
        _emit({"event": "content_report", "modes": report})
        return 0 if all(s.ok for s in contents.values()) else 1

    try:
        config = _config_from_args(args)
        server = create_server(config, contents, host=args.host, port=args.port,
                               extra_origins=args.allowed_origin, allow_lan=args.allow_lan,
                               web_dir=args.web_dir)
    except ValueError as exc:
        _emit({"event": "error", "error": "invalid_config", "detail": str(exc)}, sys.stderr)
        return 2
    except OSError as exc:
        _emit({"event": "error", "error": "bind_failed", "detail": exc.strerror or "OSError"}, sys.stderr)
        return 2

    host, port = server.server_address[:2]
    _emit({
        "event": "listening",
        "protocol": PROTOCOL,
        "host": host,
        "port": port,
        "url": "http://%s:%d" % (host, port),
        "content": dict((mode, s.status) for mode, s in contents.items()),
        "persistence": "memory-only: all rooms are lost when this process stops",
        "web": "allowlist" if args.web_dir is not None else "disabled",
    })

    def _terminate(_signum, _frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _terminate)

    if args.exit_on_stdin_eof:
        def _watch_stdin():
            try:
                while sys.stdin.buffer.read(4096):
                    pass
            except (OSError, ValueError):
                pass
            server.shutdown()
        threading.Thread(target=_watch_stdin, name="stdin-watch", daemon=True).start()

    server.app.manager.start()
    try:
        server.serve_forever(poll_interval=0.2)
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        server.app.manager.shutdown()
        server.server_close()
    _emit({"event": "stopped"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
