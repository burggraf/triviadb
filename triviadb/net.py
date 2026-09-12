"""Small bounded HTTP helpers; downloads become visible only after completion."""

import hashlib
import json
import math
import os
import time
import urllib.error
import urllib.request
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit

from .store import checkpoint, digest, dumps

USER_AGENT = os.environ.get("TRIVIADB_USER_AGENT", "TriviaDB/0.1 (local open-data trivia builder)")


class Paused(RuntimeError):
    """Safe to rerun later; committed work is retained."""


def retry_after_seconds(value, now):
    try:
        seconds = float(value)
    except (ValueError, TypeError):
        try:
            seconds = parsedate_to_datetime(value).timestamp() - now
        except (ValueError, TypeError, OverflowError):
            seconds = 60
    return max(60, seconds) if math.isfinite(seconds) else 60


class HTTPFailure(RuntimeError):
    def __init__(self, code, body=None, retry_after=None):
        super().__init__(f"HTTP {code}")
        self.code, self.body, self.retry_after = code, body or {}, retry_after


def open_url(url, data=None, headers=None):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT, **(headers or {})})
    try:
        return urllib.request.urlopen(req, timeout=90)
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read(65536))
        except (ValueError, OSError):
            body = {}
        raise HTTPFailure(exc.code, body, exc.headers.get("Retry-After")) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise HTTPFailure(0) from None


def request_json(url, data=None, headers=None):
    payload = dumps(data).encode() if data is not None else None
    with open_url(url, payload, {"Accept": "application/json", "Content-Type": "application/json", **(headers or {})}) as response:
        raw = response.read(20_000_001)
    if len(raw) > 20_000_000:
        raise ValueError("HTTP JSON response exceeds 20 MB")
    try:
        return json.loads(raw)
    except ValueError:
        raise ValueError("Server returned invalid JSON") from None


def download(url, path, max_bytes=1_000_000_000):
    path = Path(path)
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".part")
    # A failed download restarts, rather than splicing bytes from different source versions.
    with open_url(url) as response, partial.open("wb") as out:
        expected = response.headers.get("Content-Length")
        if expected and int(expected) > max_bytes:
            raise ValueError("Download exceeds configured size limit")
        total, sha = 0, hashlib.sha256()
        while chunk := response.read(256 * 1024):
            total += len(chunk)
            if total > max_bytes:
                raise ValueError("Download exceeds configured size limit")
            out.write(chunk)
            sha.update(chunk)
        if expected and total != int(expected):
            raise ValueError("Incomplete download; retry to download again")
        out.flush()
        os.fsync(out.fileno())
    if not total:
        raise ValueError("Empty download")
    partial.replace(path)
    path.with_name(path.name + ".source.json").write_text(dumps({
        "url": url, "sha256": sha.hexdigest(), "bytes": total, "retrieved_at": time.time(),
    }) + "\n", encoding="utf-8")
    return path


class CachedHTTP:
    def __init__(self, db, directory):
        self.db, self.directory = db, Path(directory)

    def get(self, url, suffix=".json", interval=1.1, max_bytes=20_000_000):
        path = self.directory / (digest(url) + suffix)
        if not path.exists():
            key = "http:" + urlsplit(url).netloc
            state = checkpoint(self.db, key)
            delay = state.get("next_at", 0) - time.time()
            if delay > 0 and state.get("paused"):
                raise Paused(f"Source {urlsplit(url).netloc} is cooling down; retry in {math.ceil(delay)} seconds.")
            if delay > 0:
                time.sleep(delay)
            with self.db:
                checkpoint(self.db, key, {"next_at": time.time() + interval})
            try:
                download(url, path, max_bytes)
            except HTTPFailure as exc:
                if exc.code == 429 or exc.code == 0 or 500 <= exc.code < 600:
                    now = time.time()
                    seconds = retry_after_seconds(exc.retry_after, now)
                    with self.db:
                        checkpoint(self.db, key, {"next_at": now + seconds, "paused": True})
                    raise Paused(f"Source {urlsplit(url).netloc} returned HTTP {exc.code}; retry after {math.ceil(seconds)} seconds.") from None
                raise
        return path

    def json(self, url, interval=1.1):
        path = self.get(url, interval=interval)
        try:
            return json.loads(path.read_text(encoding="utf-8")), str(path)
        except ValueError:
            path.unlink(missing_ok=True)  # Do not pin an HTML error page forever.
            raise ValueError("Source returned invalid JSON; rerun to retry") from None
