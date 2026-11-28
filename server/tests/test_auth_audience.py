"""剧创鉴权的 audience 白名单（2026-11-28 立）。

未配 `FD_JUCHUANG_AUTH_AUDIENCE` 时 `verify_aud` 是关的，而 JWKS 指向整个 Logto
租户 —— 于是**任何**本租户签发的 JWT 都能进剧创。这组测钉三件事：

1. 逗号分隔能解析成白名单（两条合法来路：网页 ID token 的 client_id、机器调方的 resource）。
2. 没配 / 只写了空白 ⇒ `None`，即"不校验"这条老行为保持不变（不给空清单，否则等于全拒）。
3. 配了之后**真的把清单传给 pyjwt 并开启 verify_aud** —— 只测解析不测传参，
   就等于没测到这个安全修复本身。
"""
from __future__ import annotations

import pytest

from fd_juchuang_api import auth


def test_unset_means_no_check(monkeypatch):
    monkeypatch.delenv("FD_JUCHUANG_AUTH_AUDIENCE", raising=False)
    assert auth._audiences() is None


def test_blank_and_commas_only_mean_no_check(monkeypatch):
    # 写成 ", ," 不能变成"校验一个空清单"——那会把所有人都拒掉
    monkeypatch.setenv("FD_JUCHUANG_AUTH_AUDIENCE", " , ,, ")
    assert auth._audiences() is None


def test_single_audience(monkeypatch):
    monkeypatch.setenv("FD_JUCHUANG_AUTH_AUDIENCE", "xlp6cjguc16zkxz7hc3d9")
    assert auth._audiences() == ["xlp6cjguc16zkxz7hc3d9"]


def test_multiple_audiences_are_trimmed(monkeypatch):
    monkeypatch.setenv(
        "FD_JUCHUANG_AUTH_AUDIENCE",
        " xlp6cjguc16zkxz7hc3d9 , https://mcp.finddatatech.cloud/api ,,",
    )
    assert auth._audiences() == [
        "xlp6cjguc16zkxz7hc3d9",
        "https://mcp.finddatatech.cloud/api",
    ]


def test_verify_passes_the_whitelist_and_keeps_aud_check_on(monkeypatch):
    import jwt

    monkeypatch.setenv("FD_JUCHUANG_AUTH_JWKS", "https://example.invalid/jwks")
    monkeypatch.setenv("FD_JUCHUANG_AUTH_AUDIENCE", "aud-a,aud-b")

    seen: dict = {}

    class _Client:
        def __init__(self, url):  # noqa: D107 - 替身
            seen["jwks_url"] = url

        def get_signing_key_from_jwt(self, token):  # noqa: D102 - 替身
            return type("K", (), {"key": "secret"})()

    def fake_decode(token, key, **kwargs):
        seen.update(kwargs)
        return {"sub": "user-1", "exp": 1}

    monkeypatch.setattr(jwt, "PyJWKClient", _Client)
    monkeypatch.setattr(jwt, "decode", fake_decode)

    assert auth._verify_jwt("tok") == "user-1"
    assert seen["audience"] == ["aud-a", "aud-b"]
    # verify_aud 必须在 options 里"没被关掉"：老实现靠塞 False 来绕过
    assert "verify_aud" not in seen["options"]


@pytest.mark.parametrize("raw,expected", [
    ("a", ["a"]),
    ("a,b", ["a", "b"]),
    ("", None),
    ("  ", None),
])
def test_parsing_table(monkeypatch, raw, expected):
    monkeypatch.setenv("FD_JUCHUANG_AUTH_AUDIENCE", raw)
    assert auth._audiences() == expected
