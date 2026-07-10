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
