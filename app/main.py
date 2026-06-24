"""News Digest アプリケーションエントリポイント。

環境変数:
    DATABASE_PATH      : SQLite DB パス（デフォルト /tmp/news_digest.db）
    SMTP_HOST          : smtp.gmail.com
    SMTP_PORT          : 587
    SMTP_USER          : xxx@gmail.com
    SMTP_PASSWORD      : アプリパスワード
    SMTP_FROM          : 送信元アドレス
    SMTP_FROM_NAME     : 表示名
    ANTHROPIC_API_KEY  : Claude Haiku で要約する場合
    OPENAI_API_KEY     : GPT-4o-mini で要約する場合
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .engine import DigestEngine
from .mailer import Mailer
from .router import router
from .searcher import NewsSearcher
from .store import Store
from .summarizer import Summarizer


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """アプリ起動時に依存コンポーネントを初期化して state に格納する。"""
    store     = Store()
    searcher  = NewsSearcher()
    summarizer = Summarizer()
    mailer    = Mailer()
    engine    = DigestEngine(store=store, searcher=searcher, summarizer=summarizer, mailer=mailer)

    app.state.store      = store
    app.state.searcher   = searcher
    app.state.summarizer = summarizer
    app.state.mailer     = mailer
    app.state.engine     = engine

    yield

    # shutdown: SQLite connection は Store.__del__ で閉じる（軽量なので省略可）


def create_app() -> FastAPI:
    app = FastAPI(
        title="News Digest",
        description="自動ニュース収集・要約・配信ダッシュボード",
        version="1.0.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(router)

    @app.get("/health", tags=["system"])
    async def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/", include_in_schema=False)
    async def root():
        from fastapi.responses import RedirectResponse
        return RedirectResponse("/login", status_code=302)

    return app


app = create_app()
