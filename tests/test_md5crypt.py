import subprocess

import pytest

from app.webshare import md5crypt


@pytest.mark.parametrize("password,salt", [
    ("password", "abcdefgh"),
    ("heslo123", "a1b2c3d4"),
    ("útěk-čšřž", "saltsalt"),
    ("x", "ab"),
    ("a-fairly-long-password-over-16-bytes", "12345678"),
])
def test_matches_openssl(password: str, salt: str):
    expected = subprocess.run(
        ["openssl", "passwd", "-1", "-salt", salt, password],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert md5crypt(password, salt) == expected


def test_salt_with_magic_prefix():
    assert md5crypt("pw", "$1$abcdefgh$junk") == md5crypt("pw", "abcdefgh")
