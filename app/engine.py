"""検索 → 要約 → フォーマット → 配信のオーケストレーション。"""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import List

from .mailer import Mailer
from .models import DeliveryRecord, NewsItem, Subscription
from .searcher import NewsSearcher
from .store import Store
from .summarizer import Summarizer


class DigestEngine:
    def __init__(
        self,
        store: Store,
        searcher: NewsSearcher,
        summarizer: Summarizer,
        mailer: Mailer,
    ) -> None:
        self.store     = store
        self.searcher  = searcher
        self.summarizer = summarizer
        self.mailer    = mailer

    # ── Core ────────────────────────────────────────────────────────

    def build_items(self, sub: Subscription) -> List[NewsItem]:
        """購読設定からニュース記事リストを構築する（送信しない）。"""
        results = self.searcher.search(sub.theme, max_results=sub.count)
        items: List[NewsItem] = []
        for r in results[: sub.count]:
            summary = self.summarizer.summarize(r.title, r.body)
            items.append(
                NewsItem(
                    theme=sub.theme,
                    subject=r.title,
                    summary=summary,
                    url=r.url,
                    published=r.published,
                    media=r.media,
                )
            )
        return items

    def format_email(self, sub: Subscription, items: List[NewsItem]) -> tuple[str, str]:
        """件名と本文を生成する。"""
        now     = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        sources = list(dict.fromkeys(it.media for it in items if it.media))
        src_str = " / ".join(sources) if sources else "—"

        subject = f"【{sub.theme}】最新情報ダイジェスト（{len(items)}件）"

        lines = [
            f"テーマ: {sub.theme}",
            f"配信日時: {now}",
            f"情報源: {src_str}",
            "",
        ]
        for i, it in enumerate(items, 1):
            date_str = it.published[:10] if it.published else ""
            lines += [
                f"{i}. {it.subject}  [{it.media}  {date_str}]",
                f"   {it.summary}",
                f"   {it.url}",
                "",
            ]

        return subject, "\n".join(lines)

    def send(self, sub: Subscription) -> dict:
        """1件の購読に対してダイジェストを生成・送信し、履歴に記録する。"""
        items   = self.build_items(sub)
        subject, body = self.format_email(sub, items)
        sources = list(dict.fromkeys(it.media for it in items if it.media))

        sent  = False
        error = ""

        if self.mailer.is_configured:
            try:
                self.mailer.send(sub.email, subject, body)
                sent = True
            except Exception as exc:
                error = str(exc)
        else:
            error = "smtp_not_configured"

        # last_sent_at を更新
        if sent:
            self.store.update(sub.id, last_sent_at=time.time())

        # 履歴に保存
        self.store.add_history(
            DeliveryRecord(
                id=uuid.uuid4().hex[:16],
                subscription_id=sub.id,
                theme=sub.theme,
                email=sub.email,
                subject=subject,
                item_count=len(items),
                sent_at=time.time(),
                success=sent,
                error=error,
                sources=sources,
            )
        )

        reason = (
            "" if sent
            else ("smtp_not_configured" if not self.mailer.is_configured else "smtp_error")
        )
        return {
            "sent":    sent,
            "email":   sub.email,
            "subject": subject,
            "body":    body,
            "items":   [it.to_dict() for it in items],
            "sources": sources,
            "reason":  reason,
            "error":   error,
        }

    def run_due(self) -> List[dict]:
        """配信タイミングに達した全購読を処理する（Cloud Scheduler から呼ぶ）。"""
        return [self.send(sub) for sub in self.store.due_subscriptions()]
