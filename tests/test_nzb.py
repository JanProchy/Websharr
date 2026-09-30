from app.nzb import build_nzb, parse_nzb, sanitize_filename


def test_roundtrip():
    nzb = build_nzb("abc123XY", "Show.S01E02.1080p.mkv", 123456789)
    payload = parse_nzb(nzb.encode())
    assert payload is not None
    assert payload.ident == "abc123XY"
    assert payload.name == "Show.S01E02.1080p.mkv"
    assert payload.size == 123456789


def test_roundtrip_special_chars():
    nzb = build_nzb("id1", 'Divný <film> & "název".mkv', 1)
    payload = parse_nzb(nzb.encode())
    assert payload is not None
    assert payload.name == 'Divný <film> & "název".mkv'


def test_roundtrip_alternates():
    nzb = build_nzb("main1", "Film.2024.mkv", 4_000_000_000, ["copy2", "copy3"])
    payload = parse_nzb(nzb.encode())
    assert payload.ident == "main1"
    assert payload.alternates == ["copy2", "copy3"]


def test_nzb_without_alternates():
    # NZBs built before alternates existed carry no websharr_alt meta.
    nzb = build_nzb("main1", "Film.2024.mkv", 1)
    assert "websharr_alt" not in nzb
    assert parse_nzb(nzb.encode()).alternates == []


def test_parse_garbage():
    assert parse_nzb(b"not xml at all") is None
    assert parse_nzb(b"<nzb></nzb>") is None


def test_sanitize_filename():
    assert sanitize_filename("a/b\\c:d.mkv") == "a_b_c_d.mkv"
    assert sanitize_filename("ok.mkv") == "ok.mkv"
    assert sanitize_filename("") == "unnamed"
    for hostile in ("../../etc/passwd", "..\\..\\x", "...", ".hidden"):
        cleaned = sanitize_filename(hostile)
        assert ".." not in cleaned
        assert "/" not in cleaned and "\\" not in cleaned
        assert not cleaned.startswith(".")


def test_sanitize_filename_keeps_extension_after_double_dot():
    """"Doctor Strange (2016) CZ dab. 1080p..mkv" (a real Webshare name) became
    "..._mkv": the ".." -> "_" replacement ate the extension dot, and *arr found
    "no files eligible for import". Runs of dots collapse to one instead."""
    assert sanitize_filename("Doctor Strange (2016) CZ dab. 1080p..mkv") == \
        "Doctor Strange (2016) CZ dab. 1080p.mkv"
    assert sanitize_filename("Film...avi") == "Film.avi"
    # still no way out of the folder
    for hostile in ("../../etc/passwd", "..\\..\\x.mkv", "...hidden.mkv"):
        cleaned = sanitize_filename(hostile)
        assert "/" not in cleaned and "\\" not in cleaned and ".." not in cleaned
        assert not cleaned.startswith(".")
