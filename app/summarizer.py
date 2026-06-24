"""LLM + ルールベース 80字要約モジュール。

優先順:
  1. Anthropic Claude Haiku（ANTHROPIC_API_KEY が設定されている場合）
  2. OpenAI GPT-4o-mini（OPENAI_API_KEY が設定されている場合）
  3. ルールベース（文単位で切り出し + 文字数制限）
"""
from __future__ import annotations

import os
import re

DEFAULT_MAX_CHARS = 80

_SYSTEM = "あなたは優秀なニュース記者です。記事を正確かつ簡潔に日本語で要約します。"

_USER_TMPL = """\
以下のニュース記事を日本語で {max_chars} 文字以内に要約してください。

ルール:
- 重要な事実（人名・数字・出来事）を含める
- 自然な日本語で書く（体言止めOK）
- {max_chars} 文字を厳守する
- 翻訳・補足は不要。記事の内容だけを要約する

タイトル: {title}
本文（先頭 600 文字）: {body}

要約（{max_chars} 文字以内）:"""


class Summarizer:
    """LLM 優先・ルールベースフォールバックの要約器。"""

    def __init__(self, max_chars: int = DEFAULT_MAX_CHARS) -> None:
        self.max_chars = max_chars
        self._provider = "rule-based"
        self._client = None
        self._setup()

    # ── setup ───────────────────────────────────────────────────────

    def _setup(self) -> None:
        # Anthropic
        key = os.getenv("ANTHROPIC_API_KEY", "")
        if key:
            try:
                import anthropic  # type: ignore
                self._client = anthropic.Anthropic(api_key=key)
                self._provider = "anthropic"
                return
            except Exception:
                pass

        # OpenAI
        key = os.getenv("OPENAI_API_KEY", "")
        if key:
            try:
                import openai  # type: ignore
                self._client = openai.OpenAI(api_key=key)
                self._provider = "openai"
                return
            except Exception:
                pass

    # ── public API ──────────────────────────────────────────────────

    @property
    def provider(self) -> str:
        return self._provider

    def summarize(self, title: str, body: str) -> str:
        """タイトルと本文から max_chars 字以内の要約を返す。"""
        if self._provider == "anthropic":
            return self._anthropic(title, body)
        if self._provider == "openai":
            return self._openai(title, body)
        return self._rule_based(title, body)

    # ── LLM backends ────────────────────────────────────────────────

    def _build_prompt(self, title: str, body: str) -> str:
        return _USER_TMPL.format(
            max_chars=self.max_chars,
            title=title[:200],
            body=(body or title)[:600],
        )

    def _anthropic(self, title: str, body: str) -> str:
        try:
            resp = self._client.messages.create(  # type: ignore[union-attr]
                model="claude-3-haiku-20240307",
                max_tokens=200,
                system=_SYSTEM,
                messages=[{"role": "user", "content": self._build_prompt(title, body)}],
            )
            return resp.content[0].text.strip()[: self.max_chars]
        except Exception:
            return self._rule_based(title, body)

    def _openai(self, title: str, body: str) -> str:
        try:
            resp = self._client.chat.completions.create(  # type: ignore[union-attr]
                model="gpt-4o-mini",
                max_tokens=200,
                messages=[
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": self._build_prompt(title, body)},
                ],
            )
            return resp.choices[0].message.content.strip()[: self.max_chars]
        except Exception:
            return self._rule_based(title, body)

    # ── Rule-based fallback ─────────────────────────────────────────

    def _rule_based(self, title: str, body: str) -> str:
        """文単位で意味のある要約を返す。

        1. body を文に分割
        2. 先頭から max_chars に収まるだけ結合
        3. 収まらなければタイトルを截断
        """
        text = body.strip() if body.strip() else title.strip()

        # 文分割（句点・感嘆符・改行）
        sentences = re.split(r"[。！？\n]+", text)
        sentences = [s.strip() for s in sentences if len(s.strip()) > 4]

        result = ""
        for s in sentences:
            candidate = result + s + "。" if result else s + "。"
            if len(candidate) <= self.max_chars:
                result = candidate
            else:
                # この文は入らない → 余白があれば切り詰めて追加
                remaining = self.max_chars - len(result)
                if remaining > 10:
                    result += s[: remaining - 1] + "…"
                break

        if not result:
            result = (text[: self.max_chars - 1] + "…") if len(text) > self.max_chars else text

        return result[: self.max_chars]
