"""On-disk cache of every raw file fetched or uploaded, written before it is parsed
(SPEC v0.2 §3.2a): ``<root>/<source>/<yyyy>/<mm>/<dd>/<name>``, dated by the day it was
fetched (IST). A mapping fix can then be re-applied without re-downloading anything
(``python -m app.jobs xbrl-reparse``).

Files are never overwritten: the same bytes under the same name are reused, and different
bytes under a taken name get a short content-hash suffix.
"""

import hashlib
import os
import re
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


class RawStoreError(OSError):
    """The cache directory is not writable (the file is then not parsed)."""


def safe_name(name: str) -> str:
    """A file name without directories or odd characters (it may come from a URL)."""
    base = name.replace("\\", "/").rsplit("/", 1)[-1].split("?", 1)[0]
    cleaned = _UNSAFE.sub("_", base).strip("._")
    return cleaned[:150] or "file"


class RawStore:
    def __init__(
        self, root: Path, clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    ) -> None:
        self.root = root
        self._clock = clock

    def save(self, source: str, name: str, content: bytes) -> Path:
        day = self._clock().astimezone(IST)
        folder = self.root / safe_name(source) / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}"
        target = folder / safe_name(name)
        try:
            folder.mkdir(parents=True, exist_ok=True)
            if target.exists():
                if target.read_bytes() == content:
                    return target
                digest = hashlib.sha256(content).hexdigest()[:12]
                target = target.with_name(f"{target.stem}.{digest}{target.suffix}")
                if target.exists():
                    return target
            fd, tmp = tempfile.mkstemp(dir=folder, prefix=".partial-")
            with os.fdopen(fd, "wb") as fh:
                fh.write(content)
            os.chmod(tmp, 0o644)
            os.replace(tmp, target)
        except OSError as exc:
            raise RawStoreError(f"raw cache not writable at {folder}: {exc.strerror}") from exc
        return target

    def read(self, path: str | Path) -> bytes:
        p = Path(path)
        resolved = (p if p.is_absolute() else self.root / p).resolve()
        if not resolved.is_relative_to(self.root.resolve()):
            raise RawStoreError(f"{path} is outside the raw cache")
        return resolved.read_bytes()

    def relative(self, path: Path) -> str:
        """Path as stored in the database (relative to the cache root, portable)."""
        return path.resolve().relative_to(self.root.resolve()).as_posix()
