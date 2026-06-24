"""データモデル定義。"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class DeliveryFrequency(str, Enum):
    HOURLY  = "hourly"
    DAILY   = "daily"
    WEEKLY  = "weekly"
    MONTHLY = "monthly"

    @property
    def interval_seconds(self) -> int:
        return {
            "hourly":  3600,
            "daily":   86400,
            "weekly":  604800,
            "monthly": 2592000,
        }[self.value]

    @classmethod
    def from_str(cls, value: str) -> "DeliveryFrequency":
        try:
            return cls(value.lower())
        except ValueError:
            raise ValueError(f"無効な配信頻度: {value!r}。 hourly/daily/weekly/monthly のいずれかを指定してください。")


@dataclass
class NewsItem:
    theme:     str
    subject:   str
    summary:   str
    url:       str
    published: str = ""
    media:     str = ""

    def to_dict(self) -> dict:
        return {
            "theme": self.theme, "subject": self.subject,
            "summary": self.summary, "url": self.url,
            "published": self.published, "media": self.media,
        }


@dataclass
class Subscription:
    id:          str
    theme:       str
    email:       str
    frequency:   str
    count:       int
    active:      bool         = True
    created_at:  float        = field(default_factory=time.time)
    last_sent_at: Optional[float] = None
    tags:        List[str]    = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "theme": self.theme, "email": self.email,
            "frequency": self.frequency, "count": self.count,
            "active": self.active, "created_at": self.created_at,
            "last_sent_at": self.last_sent_at, "tags": self.tags,
        }


@dataclass
class DeliveryRecord:
    id:              str
    subscription_id: str
    theme:           str
    email:           str
    subject:         str
    item_count:      int
    sent_at:         float
    success:         bool
    error:           str       = ""
    sources:         List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "subscription_id": self.subscription_id,
            "theme": self.theme, "email": self.email,
            "subject": self.subject, "item_count": self.item_count,
            "sent_at": self.sent_at, "success": self.success,
            "error": self.error, "sources": self.sources,
        }
