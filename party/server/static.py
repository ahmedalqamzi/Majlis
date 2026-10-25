"""Optional same-origin static adapter for the ``party/web`` frontend.

Disabled unless ``--web-dir`` is given. Only an exact allowlist of request
paths is served; everything else is ``not_found``. There is no directory
listing, no extension-based MIME guessing and no path normalisation: the raw
request path must equal an allowlisted key byte-for-byte.

Filesystem access is descriptor-based. The root directory is opened once at
startup and kept open; each file is opened relative to it one component at a
time with ``O_NOFOLLOW``, so every symlink (file or directory, inside or
outside the root) is refused, and replacing a directory with a symlink after
startup cannot redirect a request outside the root. Only regular files up to
``MAX_FILE_BYTES`` are served.
"""

import os
import re
import stat

HTML = "text/html; charset=utf-8"
JS = "text/javascript; charset=utf-8"
CSS = "text/css; charset=utf-8"

# Request path -> (relative components, content type). Mirrors the production
# frontend: four entry pages, party.css and the ES modules they import.
# Excluded on purpose: js/sim-transport.js, sim/, tests/, *.md, content packs.
FILES = {
    "/": (("index.html",), HTML),
    "/index.html": (("index.html",), HTML),
    "/host.html": (("host.html",), HTML),
    "/play.html": (("play.html",), HTML),
    "/tv.html": (("tv.html",), HTML),
    "/css/party.css": (("css", "party.css"), CSS),
}
for _name in ("index", "host", "play", "tv", "ui", "transport", "session", "modes", "views", "qr"):
    FILES["/js/%s.js" % _name] = (("js", _name + ".js"), JS)
del _name

# The only accepted query: play.html?code=<room code> (QR join links).
PLAY_QUERY = re.compile(r"code=[ABCDEFGHJKLMNPQRSTUVWXYZ23456789]{6}")

MAX_FILE_BYTES = 2 * 1024 * 1024

HTML_CSP = ("default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; "
            "form-action 'self'; frame-ancestors 'none'")

_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)


def lookup(path):
    """``(parts, content_type)`` for an allowlisted raw path, else None."""
    return FILES.get(path)


def query_allowed(path, query):
    return path == "/play.html" and PLAY_QUERY.fullmatch(query) is not None


class StaticRoot(object):
    """An open root directory descriptor. Messages never contain paths."""

    def __init__(self, directory):
        if not _O_NOFOLLOW or os.open not in os.supports_dir_fd:
            raise ValueError("--web-dir is not supported on this platform")
        try:
            fd = os.open(directory, os.O_RDONLY | _O_DIRECTORY | _O_CLOEXEC)
        except (OSError, TypeError, ValueError):
            raise ValueError("--web-dir must be an existing directory")
        try:
            if not stat.S_ISDIR(os.fstat(fd).st_mode):
                raise ValueError("--web-dir must be an existing directory")
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd

    def close(self):
        fd, self._fd = self._fd, None
        if fd is not None:
            os.close(fd)

    def read(self, parts):
        """File bytes, or None for anything missing, non-regular, oversized or
        reached through a symlink."""
        if self._fd is None:
            return None
        opened = []
        try:
            dir_fd = self._fd
            for name in parts[:-1]:
                dir_fd = os.open(name, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC, dir_fd=dir_fd)
                opened.append(dir_fd)
                if not stat.S_ISDIR(os.fstat(dir_fd).st_mode):
                    return None
            fd = os.open(parts[-1], os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK | _O_CLOEXEC, dir_fd=dir_fd)
            opened.append(fd)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
                return None
            chunks = []
            total = 0
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_FILE_BYTES:
                    return None
                chunks.append(chunk)
            return b"".join(chunks)
        except OSError:
            return None
        finally:
            for fd in reversed(opened):
                try:
                    os.close(fd)
                except OSError:
                    pass
