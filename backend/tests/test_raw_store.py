"""Raw-file cache (SPEC §3.2a): data/raw/<source>/<yyyy>/<mm>/<dd>/<name>, never overwritten."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.data.raw_store import RawStore, RawStoreError, safe_name

# 20:00 UTC on 9 May = 01:30 IST on 10 May: dated by the IST day
CLOCK = datetime(2024, 5, 9, 20, 0, tzinfo=UTC)


def store(root: Path) -> RawStore:
    return RawStore(root, clock=lambda: CLOCK)


def test_layout_is_source_and_ist_date(tmp_path: Path) -> None:
    s = store(tmp_path)
    p = s.save("nse", "https://nsearchives.nseindia.com/corporate/xbrl/INDAS_1_WEB.xml", b"<x/>")
    assert p == tmp_path / "nse" / "2024" / "05" / "10" / "INDAS_1_WEB.xml"
    assert p.read_bytes() == b"<x/>" and s.relative(p) == "nse/2024/05/10/INDAS_1_WEB.xml"
    assert s.read("nse/2024/05/10/INDAS_1_WEB.xml") == b"<x/>"
    assert not list(p.parent.glob(".partial-*"))  # written atomically


def test_never_overwrites(tmp_path: Path) -> None:
    s = store(tmp_path)
    first = s.save("nse", "a.xml", b"one")
    assert s.save("nse", "a.xml", b"one") == first  # same bytes: reused
    second = s.save("nse", "a.xml", b"two")  # a revised document under the same name
    assert second != first and second.name.startswith("a.") and second.suffix == ".xml"
    assert (first.read_bytes(), second.read_bytes()) == (b"one", b"two")


@pytest.mark.parametrize(
    ("name", "safe"),
    [("../../etc/passwd", "passwd"), ("a b?x=1", "a_b"), ("", "file"), ("..", "file"),
     ("https://h/p/INDAS.xml?sig=abc", "INDAS.xml")],
)  # fmt: skip
def test_safe_names(name: str, safe: str) -> None:
    assert safe_name(name) == safe


def test_reads_stay_inside_the_cache(tmp_path: Path) -> None:
    s = store(tmp_path / "raw")
    (tmp_path / "secret").write_text("x")
    with pytest.raises(RawStoreError, match="outside the raw cache"):
        s.read("../secret")


def test_unwritable_cache_raises(tmp_path: Path) -> None:
    blocker = tmp_path / "raw"
    blocker.write_text("a file where the cache directory should be")
    with pytest.raises(RawStoreError, match="raw cache not writable"):
        store(blocker).save("nse", "a.xml", b"x")
