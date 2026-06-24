"""DuckDuckGo ニュース検索クライアント（日本優先）。"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List


@dataclass
class SearchResult:
    title:     str
    url:       str
    body:      str
    media:     str = ""
    published: str = ""


class NewsSearcher:
    """DuckDuckGo News 検索。

    `region="jp-jp"` により日本メディア（NHK / 日経 / 朝日等）を優先する。
    v7+ ライブラリで region が使えない場合は自動フォールバック。
    """

    def __init__(self, region: str = "jp-jp") -> None:
        self.region = region
        self._ddgs = None
        # ddgs (旧 duckduckgo-search) → ddgs パッケージを優先
        for pkg in ("ddgs", "duckduckgo_search"):
            try:
                mod = __import__(pkg, fromlist=["DDGS"])
                self._ddgs = mod.DDGS()
                break
            except (ImportError, Exception):
                continue

    @property
    def is_native(self) -> bool:
        return self._ddgs is not None

    def search(self, query: str, max_results: int = 5) -> List[SearchResult]:
        """ニュースを検索して SearchResult リストを返す。"""
        if not self._ddgs:
            return self._mock(query, max_results)

        raw: List[dict] = []
        try:
            raw = list(self._ddgs.news(query, region=self.region, max_results=max_results))
        except TypeError:
            # v7+: region 引数が無い場合のフォールバック
            try:
                raw = list(self._ddgs.news(query, max_results=max_results))
            except Exception:
                pass
        except Exception:
            pass

        if not raw:
            return self._mock(query, max_results)

        return [
            SearchResult(
                title=r.get("title", ""),
                url=r.get("url", ""),
                body=r.get("body", ""),
                media=r.get("source", ""),
                published=r.get("date", ""),
            )
            for r in raw
        ]

    def _mock(self, query: str, n: int) -> List[SearchResult]:
        return [
            SearchResult(
                title=f"[Mock] {query} に関するニュース {i}",
                url="https://example.com",
                body=f"{query} についてのモック記事本文 {i}。",
                media="Mock Media",
                published="",
            )
            for i in range(1, min(n, 3) + 1)
        ]
