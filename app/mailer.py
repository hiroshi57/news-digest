"""SMTP メール送信クライアント。"""
from __future__ import annotations

import os
import smtplib
from email.mime.text import MIMEText
from email.utils import formataddr


class Mailer:
    """環境変数から SMTP 設定を読み取り送信するクライアント。

    必要な環境変数:
        SMTP_HOST      : smtp.gmail.com
        SMTP_PORT      : 587（デフォルト）
        SMTP_USER      : xxx@gmail.com
        SMTP_PASSWORD  : アプリパスワード
        SMTP_FROM      : 送信元アドレス（省略時は SMTP_USER）
        SMTP_FROM_NAME : 表示名（デフォルト: "News Digest"）
    """

    def __init__(self) -> None:
        self.host      = os.getenv("SMTP_HOST", "")
        self.port      = int(os.getenv("SMTP_PORT", "587"))
        self.user      = os.getenv("SMTP_USER", "")
        self.password  = os.getenv("SMTP_PASSWORD", "")
        self.from_addr = os.getenv("SMTP_FROM") or self.user
        self.from_name = os.getenv("SMTP_FROM_NAME", "News Digest")

    @property
    def is_configured(self) -> bool:
        return bool(self.host and self.user and self.password)

    def send(self, to: str, subject: str, body: str) -> dict:
        """メールを送信する。成功時 {"ok": True, "to": to} を返す。"""
        if not self.is_configured:
            raise RuntimeError("SMTP が設定されていません（SMTP_HOST / SMTP_USER / SMTP_PASSWORD）")

        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"]    = formataddr((self.from_name, self.from_addr))
        msg["To"]      = to

        with smtplib.SMTP(self.host, self.port, timeout=20) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(self.user, self.password)
            server.sendmail(self.from_addr, [to], msg.as_string())

        return {"ok": True, "to": to}
