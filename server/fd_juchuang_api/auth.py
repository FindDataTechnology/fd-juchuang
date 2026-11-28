"""个人层访问控制（task 3.3）：必须携带用户授权，未授权拒绝并留痕。

两种令牌形态（都算「用户授权」）：
- JWKS JWT（RS256）：设 FD_JUCHUANG_AUTH_JWKS（如 Logto 的 JWKS 端点，6.3 接线）
  + FD_JUCHUANG_AUTH_AUDIENCE 时启用，sub 即 user_id。
- 开发态固定令牌：FD_JUCHUANG_AUTH_DEV_TOKENS="token1=user_a,token2=user_b"
  （本地联调与测试用；生产不设此变量即只剩 JWT 一条路）。
"""

from __future__ import annotations

import os

from fastapi import Depends, HTTPException, Request

from . import personal


class Unauthorized(HTTPException):
    def __init__(self, detail: str):
        super().__init__(status_code=401, detail=detail)


def _dev_tokens() -> dict[str, str]:
    raw = os.environ.get("FD_JUCHUANG_AUTH_DEV_TOKENS", "")
    out: dict[str, str] = {}
    for pair in raw.split(","):
        if "=" in pair:
            token, user = pair.split("=", 1)
            out[token.strip()] = user.strip()
    return out


def _audiences() -> list[str] | None:
    """允许逗号分隔的多个 audience；空则返回 None（表示不校验）。

    **为什么必须配**：不配的时候 `verify_aud` 是关的，而 `FD_JUCHUANG_AUTH_JWKS`
    指向的是整个 Logto 租户的 JWKS —— 于是**任何**一条本租户签发的 JWT 都能进
    剧创，`sub` 被当成 user_id。实际有两条合法来路，aud 各不相同：

      - 网页用户：ID token（scope 只有 openid/profile/offline_access，无 resource）
        ⇒ aud = client_id `xlp6cjguc16zkxz7hc3d9`（线上 bundle 实测）
      - 机器调方：client_credentials（resource=https://mcp.finddatatech.cloud/api）
        ⇒ aud = 那个 resource

    只认一个会把另一条路打死；所以这里收一个**白名单**，逗号分隔。
    """
    raw = os.environ.get("FD_JUCHUANG_AUTH_AUDIENCE", "")
    vals = [a.strip() for a in raw.split(",") if a.strip()]
    return vals or None


def _verify_jwt(token: str) -> str:
    import jwt  # pyjwt

    jwks_url = os.environ.get("FD_JUCHUANG_AUTH_JWKS", "")
    audience = _audiences()
    if not jwks_url:
        raise Unauthorized("JWT 校验未配置（FD_JUCHUANG_AUTH_JWKS）")
    from jwt import PyJWKClient

    jwks = PyJWKClient(jwks_url)
    signing = jwks.get_signing_key_from_jwt(token)
    # Logto 1.42 签发 ES384（实测 demo-user ID token）；RS256 保留兼容自签场景。
    # 未配置 audience 时显式关 aud 校验：pyjwt 2.10+ 对「token 带 aud 但调用方未传」
    # 也执行校验并抛 InvalidAudienceError（线上实测第二个坑）。
    claims = jwt.decode(
        token,
        signing.key,
        algorithms=["RS256", "ES384"],
        audience=audience or None,
        options={
            "require": ["exp", "sub"],
            **({} if audience else {"verify_aud": False}),
        },
    )
    return str(claims["sub"])


def authenticate(request: Request) -> str:
    """从 Authorization: Bearer 解出 user_id；失败即 401 并写审计留痕。"""
    header = request.headers.get("authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""

    user_id: str | None = None
    deny_reason = "missing bearer token"
    if token:
        dev = _dev_tokens()
        if token in dev:
            user_id = dev[token]
        else:
            try:
                user_id = _verify_jwt(token)
            except Exception as exc:  # noqa: BLE001 —— 任何校验失败都归一为 401
                deny_reason = f"invalid token: {type(exc).__name__}"

    conn = personal.connect()
    try:
        personal.migrate(conn)
        if user_id:
            personal.audit(conn, user_id, "auth", request.url.path, "allowed")
            return user_id
        personal.audit(conn, None, "auth", request.url.path, "denied", detail=deny_reason)
        raise Unauthorized(deny_reason)
    finally:
        conn.close()


def require_user(user_id: str = Depends(authenticate)) -> str:
    return user_id
