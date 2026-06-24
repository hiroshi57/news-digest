"""マジックリンク認証ユーティリティ。

フロー:
  1. POST /login にメールアドレスを送信
  2. 署名付き magic token をメールで送信（15 分有効）
  3. GET /auth/verify?token=... でトークン検証 → セッション Cookie 発行
  4. Cookie を持っている間はログイン状態を維持（デフォルト 7 日）

必要な環境変数:
  SECRET_KEY        : セッション署名キー（必ず変更すること）
  ADMIN_EMAIL       : 全購読を管理できる管理者メール（省略時 SMTP_FROM）
  BASE_URL          : マジックリンクの URL プレフィックス
  MAGIC_LINK_EXPIRES: magic token 有効秒数（デフォルト 900 = 15 分）
  SESSION_EXPIRES   : セッション有効秒数（デフォルト 604800 = 7 日）
"""
from __future__ import annotations

import os
import secrets
from typing import Optional

from fastapi import Request

try:
    from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
    _HAS_ITS = True
except ImportError:
    _HAS_ITS = False

# ── 設定 ───────────────────────────────────────────────────────────

SECRET_KEY: str = os.getenv("SECRET_KEY", secrets.token_hex(32))
MAGIC_LINK_EXPIRES: int = int(os.getenv("MAGIC_LINK_EXPIRES", "900"))     # 15 分
SESSION_EXPIRES:    int = int(os.getenv("SESSION_EXPIRES",    "604800"))   # 7 日
ADMIN_EMAIL: str = (os.getenv("ADMIN_EMAIL") or os.getenv("SMTP_FROM") or "").lower()
BASE_URL: str    = os.getenv("BASE_URL", "").rstrip("/")

SESSION_COOKIE = "nd_session"

# ── 署名器 ─────────────────────────────────────────────────────────

_serializer: Optional[object] = None

def _get_serializer():
    global _serializer
    if not _HAS_ITS:
        return None
    if _serializer is None:
        _serializer = URLSafeTimedSerializer(SECRET_KEY)
    return _serializer


# ── token API ─────────────────────────────────────────────────────

def make_magic_token(email: str) -> str:
    """メールアドレスを署名した magic token を返す。"""
    s = _get_serializer()
    if s is None:
        # fallback: token は使い捨て secrets (DB 不要の簡易実装では期限なし)
        return secrets.token_urlsafe(32) + ":" + email
    return s.dumps(email.lower(), salt="magic-link")  # type: ignore[union-attr]


def verify_magic_token(token: str) -> Optional[str]:
    """有効な token なら email を返す。無効/期限切れなら None。"""
    s = _get_serializer()
    if s is None:
        # fallback: "token:email" 形式
        parts = token.split(":", 1)
        return parts[1].lower() if len(parts) == 2 else None
    try:
        return s.loads(token, salt="magic-link", max_age=MAGIC_LINK_EXPIRES)  # type: ignore[union-attr]
    except (SignatureExpired, BadSignature):
        return None


def make_session_token(email: str) -> str:
    """セッション Cookie 値を返す。"""
    s = _get_serializer()
    if s is None:
        return secrets.token_urlsafe(32) + ":" + email.lower()
    return s.dumps(email.lower(), salt="session")  # type: ignore[union-attr]


def verify_session_token(token: str) -> Optional[str]:
    """有効なセッション token なら email を返す。"""
    s = _get_serializer()
    if s is None:
        parts = token.split(":", 1)
        return parts[1].lower() if len(parts) == 2 else None
    try:
        return s.loads(token, salt="session", max_age=SESSION_EXPIRES)  # type: ignore[union-attr]
    except (SignatureExpired, BadSignature):
        return None


# ── helpers ────────────────────────────────────────────────────────

def magic_link_url(request: Request, token: str) -> str:
    """request からベース URL を解決して magic link URL を組み立てる。"""
    base = BASE_URL or str(request.base_url).rstrip("/")
    return f"{base}/auth/verify?token={token}"


def get_session_email(request: Request) -> Optional[str]:
    """Cookie からセッションを読んでメールアドレスを返す。未ログインなら None。"""
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    return verify_session_token(token)


def is_admin(email: str) -> bool:
    """管理者メールかどうかを判定する。"""
    return bool(ADMIN_EMAIL) and email.lower() == ADMIN_EMAIL


# ── メール本文 ─────────────────────────────────────────────────────

def magic_link_email_body(link: str) -> tuple[str, str]:
    """(subject, body) を返す。"""
    subject = "【News Digest】ログインリンク"
    body = f"""\
News Digest へのログインリンクをお送りします。

以下のリンクをクリックしてログインしてください（{MAGIC_LINK_EXPIRES // 60} 分間有効）:

{link}

このメールに心当たりがない場合は、無視してください。
リンクをクリックしない限りログインは行われません。

──────────────────────────
News Digest
"""
    return subject, body
