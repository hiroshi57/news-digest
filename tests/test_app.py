"""News Digest 基本テスト。

テスト方針:
- 外部 I/O (SMTP / DuckDuckGo / LLM) はモック or stub で隔離
- SQLite は in-memory (:memory:) を使用
- FastAPI TestClient で HTTP エンドポイントを結合テスト
"""
from __future__ import annotations

import json
import os
import sys

import pytest

# ── テスト前に DB を in-memory にする ────────────────────────────────
os.environ.setdefault("DATABASE_PATH", ":memory:")

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

from app.models import DeliveryFrequency, NewsItem, Subscription
from app.store import Store
from app.summarizer import Summarizer
from app.searcher import NewsSearcher, SearchResult


# ════════════════════════════════════════════════════════════════════
# models
# ════════════════════════════════════════════════════════════════════


class TestDeliveryFrequency:
    def test_from_str_daily(self) -> None:
        freq = DeliveryFrequency.from_str("daily")
        assert freq == DeliveryFrequency.DAILY

    def test_from_str_unknown_raises(self) -> None:
        with pytest.raises(ValueError):
            DeliveryFrequency.from_str("unknown_value")

    def test_interval_daily(self) -> None:
        assert DeliveryFrequency.DAILY.interval_seconds == 86400

    def test_interval_hourly(self) -> None:
        assert DeliveryFrequency.HOURLY.interval_seconds == 3600


class TestNewsItem:
    def test_to_dict_has_expected_keys(self) -> None:
        item = NewsItem(
            theme="AI",
            subject="テストタイトル",
            summary="テスト要約",
            url="https://example.com",
            published="2026-06-24",
            media="NHK",
        )
        d = item.to_dict()
        assert d["theme"] == "AI"
        assert d["subject"] == "テストタイトル"
        assert d["summary"] == "テスト要約"
        assert d["url"] == "https://example.com"
        assert d["media"] == "NHK"


# ════════════════════════════════════════════════════════════════════
# Store (SQLite in-memory)
# ════════════════════════════════════════════════════════════════════


class TestStore:
    def setup_method(self) -> None:
        # 各テストで新鮮な in-memory DB を使用
        self.store = Store(db_path=":memory:")

    def teardown_method(self) -> None:
        self.store.close()

    def _make_sub(self, theme: str = "AI", email: str = "test@example.com") -> Subscription:
        return self.store.create(theme=theme, email=email, frequency="daily", count=5)

    def test_create_and_get(self) -> None:
        sub = self._make_sub()
        fetched = self.store.get(sub.id)
        assert fetched is not None
        assert fetched.theme == "AI"
        assert fetched.email == "test@example.com"

    def test_list_all(self) -> None:
        self._make_sub("AI")
        self._make_sub("経済")
        subs = self.store.list_all()
        assert len(subs) == 2

    def test_update_active(self) -> None:
        sub = self._make_sub()
        self.store.update(sub.id, active=False)
        fetched = self.store.get(sub.id)
        assert fetched is not None
        assert fetched.active is False

    def test_delete(self) -> None:
        sub = self._make_sub()
        self.store.delete(sub.id)
        assert self.store.get(sub.id) is None

    def test_bulk_update_active(self) -> None:
        s1 = self._make_sub("AI")
        s2 = self._make_sub("経済")
        self.store.bulk_update([s1.id, s2.id], active=False)
        for sid in [s1.id, s2.id]:
            assert self.store.get(sid).active is False  # type: ignore[union-attr]

    def test_bulk_delete(self) -> None:
        s1 = self._make_sub("AI")
        s2 = self._make_sub("経済")
        self.store.bulk_delete([s1.id, s2.id])
        assert self.store.list_all() == []

    def test_export_import_json(self) -> None:
        self._make_sub("AI")
        exported = self.store.export_json()
        data = json.loads(exported)
        # export_json は list を返す
        assert isinstance(data, list)
        assert len(data) == 1

        # 別の Store にインポート（import_json は追加件数 int を返す）
        store2 = Store(db_path=":memory:")
        count = store2.import_json(exported)
        assert count == 1
        assert len(store2.list_all()) == 1

    def test_add_and_list_history(self) -> None:
        from app.models import DeliveryRecord
        sub = self._make_sub()
        record = DeliveryRecord(
            id="test001",
            subscription_id=sub.id,
            theme="AI",
            email="test@example.com",
            subject="テスト件名",
            item_count=3,
            sent_at=1700000000.0,
            success=True,
            error="",
            sources=["NHK"],
        )
        self.store.add_history(record)
        history = self.store.list_history()
        assert len(history) == 1
        assert history[0].theme == "AI"

    def test_history_stats(self) -> None:
        from app.models import DeliveryRecord
        sub = self._make_sub()
        for i in range(3):
            self.store.add_history(DeliveryRecord(
                id=f"h{i}",
                subscription_id=sub.id,
                theme="AI",
                email="test@example.com",
                subject=f"件名{i}",
                item_count=5,
                sent_at=1700000000.0 + i,
                success=(i < 2),
                error="" if i < 2 else "error",
                sources=[],
            ))
        stats = self.store.history_stats()
        # history_stats は total_deliveries(success件数), total_articles, unique_themes, last_sent_at を返す
        assert stats["total_deliveries"] == 2   # success=True は 2件
        assert stats["total_articles"] == 10    # 5 articles × 2 successful
        assert stats["unique_themes"] == 1


# ════════════════════════════════════════════════════════════════════
# Summarizer (rule-based; LLM API は呼ばない)
# ════════════════════════════════════════════════════════════════════


class TestSummarizer:
    def setup_method(self) -> None:
        # API キーを外してルールベースを強制
        os.environ.pop("ANTHROPIC_API_KEY", None)
        os.environ.pop("OPENAI_API_KEY", None)
        self.s = Summarizer(max_chars=80)

    def test_provider_is_rule_based(self) -> None:
        assert self.s.provider == "rule-based"

    def test_summary_within_max_chars(self) -> None:
        body = "人工知能技術が急速に進化している。多くの企業がAI導入を加速している。競争は激しくなる一方だ。"
        result = self.s.summarize("AI最前線", body)
        assert len(result) <= 80

    def test_empty_body_falls_back_to_title(self) -> None:
        result = self.s.summarize("タイトルだけ", "")
        assert "タイトルだけ" in result or len(result) > 0

    def test_long_text_truncated(self) -> None:
        body = "あ" * 200
        result = self.s.summarize("長文テスト", body)
        assert len(result) <= 80


# ════════════════════════════════════════════════════════════════════
# NewsSearcher (mock fallback)
# ════════════════════════════════════════════════════════════════════


class TestNewsSearcher:
    def test_mock_returns_results(self) -> None:
        searcher = NewsSearcher()
        # _ddgs が None になるように差し替えて mock を強制
        searcher._ddgs = None
        results = searcher.search("テスト", max_results=3)
        assert len(results) == 3
        assert all(isinstance(r, SearchResult) for r in results)

    def test_search_result_fields(self) -> None:
        searcher = NewsSearcher()
        searcher._ddgs = None
        results = searcher.search("経済", max_results=1)
        r = results[0]
        assert r.title
        assert r.url
        assert r.body


# ════════════════════════════════════════════════════════════════════
# FastAPI エンドポイント結合テスト
# ════════════════════════════════════════════════════════════════════


TEST_USER_EMAIL = "testuser@example.com"
TEST_ADMIN_EMAIL = "admin@example.com"


def _make_session_cookie(email: str) -> str:
    """テスト用セッション Cookie 値を生成する。"""
    from app.auth import make_session_token
    return make_session_token(email)


@pytest.fixture()
def raw_client():
    """認証なしの TestClient（/health などのパブリックエンドポイント用）。"""
    from unittest.mock import MagicMock
    from fastapi.testclient import TestClient
    from app.main import create_app

    _app = create_app()
    mock_searcher = MagicMock()
    mock_searcher.search.return_value = [
        SearchResult("テストタイトル", "https://example.com", "本文テスト", "NHK", "2026-06-24")
    ]
    mock_mailer = MagicMock()
    mock_mailer.is_configured = False

    with TestClient(_app) as c:
        c.app.state.searcher = mock_searcher
        c.app.state.mailer   = mock_mailer
        engine = c.app.state.engine
        engine.searcher = mock_searcher
        engine.mailer   = mock_mailer
        yield c


@pytest.fixture()
def client():
    """一般ユーザーとしてログイン済みの TestClient。"""
    from unittest.mock import MagicMock
    from fastapi.testclient import TestClient
    from app.main import create_app
    from app.auth import SESSION_COOKIE

    _app = create_app()
    mock_searcher = MagicMock()
    mock_searcher.search.return_value = [
        SearchResult("テストタイトル", "https://example.com", "本文テスト", "NHK", "2026-06-24")
    ]
    mock_mailer = MagicMock()
    mock_mailer.is_configured = False

    with TestClient(_app) as c:
        c.app.state.searcher = mock_searcher
        c.app.state.mailer   = mock_mailer
        engine = c.app.state.engine
        engine.searcher = mock_searcher
        engine.mailer   = mock_mailer
        # セッション Cookie を設定
        c.cookies.set(SESSION_COOKIE, _make_session_cookie(TEST_USER_EMAIL))
        yield c


@pytest.fixture()
def admin_client():
    """管理者としてログイン済みの TestClient。"""
    from unittest.mock import MagicMock
    from fastapi.testclient import TestClient
    from app.main import create_app
    from app.auth import SESSION_COOKIE

    _app = create_app()
    mock_searcher = MagicMock()
    mock_searcher.search.return_value = [
        SearchResult("テストタイトル", "https://example.com", "本文テスト", "NHK", "2026-06-24")
    ]
    mock_mailer = MagicMock()
    mock_mailer.is_configured = False

    with TestClient(_app, base_url="http://testserver") as c:
        c.app.state.searcher = mock_searcher
        c.app.state.mailer   = mock_mailer
        engine = c.app.state.engine
        engine.searcher = mock_searcher
        engine.mailer   = mock_mailer
        # 管理者セッション Cookie + ADMIN_EMAIL を設定
        os.environ["ADMIN_EMAIL"] = TEST_ADMIN_EMAIL
        c.cookies.set(SESSION_COOKIE, _make_session_cookie(TEST_ADMIN_EMAIL))
        yield c
    os.environ.pop("ADMIN_EMAIL", None)


class TestHealthEndpoint:
    def test_healthz(self, raw_client) -> None:
        resp = raw_client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


# ════════════════════════════════════════════════════════════════════
# 認証テスト
# ════════════════════════════════════════════════════════════════════

class TestAuth:
    def test_unauthenticated_subscription_returns_401(self, raw_client) -> None:
        resp = raw_client.get("/v1/subscriptions")
        assert resp.status_code == 401

    def test_login_page_returns_html(self, raw_client) -> None:
        resp = raw_client.get("/login")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")
        assert "ログイン" in resp.text

    def test_magic_token_verify_redirects_to_dashboard(self, raw_client) -> None:
        from app.auth import make_magic_token
        token = make_magic_token("newuser@example.com")
        resp = raw_client.get(f"/auth/verify?token={token}", follow_redirects=False)
        assert resp.status_code in (302, 303)
        assert "/dashboard" in resp.headers.get("location", "")

    def test_invalid_token_redirects_to_login(self, raw_client) -> None:
        resp = raw_client.get("/auth/verify?token=invalidtoken", follow_redirects=False)
        assert resp.status_code in (302, 303)
        assert "/login" in resp.headers.get("location", "")

    def test_logout_clears_cookie(self, client) -> None:
        resp = client.get("/logout", follow_redirects=False)
        assert resp.status_code in (302, 303)


class TestSubscriptionEndpoints:
    def test_create_subscription(self, client) -> None:
        resp = client.post("/v1/subscriptions", json={
            "theme": "テストAI",
            "email": "test@example.com",
            "frequency": "daily",
            "count": 5,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["theme"] == "テストAI"
        assert data["email"] == "test@example.com"
        assert "id" in data
        assert data["owner_email"] == TEST_USER_EMAIL

    def test_list_subscriptions(self, client) -> None:
        client.post("/v1/subscriptions", json={
            "theme": "経済", "email": TEST_USER_EMAIL, "frequency": "daily", "count": 3
        })
        resp = client.get("/v1/subscriptions")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        # owner_email フィルタが効いているか確認
        for s in data:
            assert s["owner_email"] == TEST_USER_EMAIL

    def test_user_cannot_delete_others_subscription(self, client) -> None:
        """同一アプリの別ユーザーセッションで 403 を確認する。"""
        from app.auth import SESSION_COOKIE, make_session_token
        # 別ユーザー (other@example.com) として購読を作成
        other_cookie = make_session_token("other@example.com")
        resp = client.post(
            "/v1/subscriptions",
            json={"theme": "他人テーマ", "email": "other@example.com",
                  "frequency": "daily", "count": 3},
            cookies={SESSION_COOKIE: other_cookie},
        )
        assert resp.status_code == 200
        sid = resp.json()["id"]
        # 自分 (TEST_USER_EMAIL) が削除しようとすると 403
        del_resp = client.delete(f"/v1/subscriptions/{sid}")
        assert del_resp.status_code == 403

    def test_delete_own_subscription(self, client) -> None:
        create_resp = client.post("/v1/subscriptions", json={
            "theme": "削除テスト", "email": TEST_USER_EMAIL, "frequency": "weekly", "count": 3
        })
        sid = create_resp.json()["id"]
        del_resp = client.delete(f"/v1/subscriptions/{sid}")
        assert del_resp.status_code == 200
        # 取得すると 404
        get_resp = client.get(f"/v1/subscriptions/{sid}")
        assert get_resp.status_code == 404


class TestPreviewEndpoint:
    def test_preview_returns_items(self, client) -> None:
        resp = client.post("/v1/preview", json={
            "theme": "AIテスト",
            "count": 1,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data
        assert len(data["items"]) >= 1


class TestHistoryEndpoint:
    def test_history_empty_initially(self, client) -> None:
        resp = client.get("/v1/history")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_history_stats(self, client) -> None:
        resp = client.get("/v1/history/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert "total_deliveries" in data
        assert "total_articles" in data


class TestDashboard:
    def test_dashboard_returns_html(self, client) -> None:
        resp = client.get("/dashboard")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")
        assert "News Digest" in resp.text
