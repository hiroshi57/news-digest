"""SQLite 永続化ストア（購読 + 配信履歴）。

DATABASE_PATH 環境変数で保存先を変更できる。
デフォルト: /tmp/news_digest.db
Cloud Run で永続化が必要な場合は Cloud SQL / Firestore へ移行する。
"""
from __future__ import annotations

import json
import os
import pathlib
import sqlite3
import time
import uuid
from typing import List, Optional

from .models import DeliveryRecord, Subscription

DATABASE_PATH = os.getenv("DATABASE_PATH", "/tmp/news_digest.db")

_INTERVALS = {
    "hourly": 3600, "daily": 86400,
    "weekly": 604800, "monthly": 2592000,
}


class Store:
    def __init__(self, db_path: str = DATABASE_PATH) -> None:
        self.db_path = db_path
        # :memory: は全操作で同一コネクションを共有する（新規接続ごとに DB が消えるため）
        self._shared_conn: Optional[sqlite3.Connection] = None
        if db_path == ":memory:":
            self._shared_conn = sqlite3.connect(":memory:", check_same_thread=False)
            self._shared_conn.row_factory = sqlite3.Row
        else:
            # ファイルパスの場合、ディレクトリが無ければ作成
            parent = pathlib.Path(db_path).parent
            try:
                parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
            # 書き込みテスト: 失敗したら :memory: にフォールバック
            try:
                test_conn = sqlite3.connect(db_path)
                test_conn.close()
            except sqlite3.OperationalError:
                import warnings
                warnings.warn(
                    f"Cannot open SQLite DB at '{db_path}', falling back to :memory:",
                    RuntimeWarning, stacklevel=2,
                )
                self.db_path = ":memory:"
                self._shared_conn = sqlite3.connect(":memory:", check_same_thread=False)
                self._shared_conn.row_factory = sqlite3.Row
        self._init_db()

    def close(self) -> None:
        """共有コネクションを明示的に閉じる（テスト終了時など）。"""
        if self._shared_conn is not None:
            self._shared_conn.close()
            self._shared_conn = None

    def __del__(self) -> None:
        self.close()

    # ── internal ────────────────────────────────────────────────────

    def _conn(self) -> sqlite3.Connection:
        if self._shared_conn is not None:
            return self._shared_conn
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        c = self._conn()
        if self._shared_conn is not None:
            # shared conn: context manager は使わず直接 execute する
            c.execute("""
                CREATE TABLE IF NOT EXISTS subscriptions (
                    id           TEXT PRIMARY KEY,
                    theme        TEXT NOT NULL,
                    email        TEXT NOT NULL,
                    frequency    TEXT NOT NULL DEFAULT 'daily',
                    count        INTEGER NOT NULL DEFAULT 5,
                    active       INTEGER NOT NULL DEFAULT 1,
                    created_at   REAL    NOT NULL,
                    last_sent_at REAL,
                    tags         TEXT    NOT NULL DEFAULT '[]'
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS delivery_history (
                    id              TEXT PRIMARY KEY,
                    subscription_id TEXT,
                    theme           TEXT,
                    email           TEXT,
                    subject         TEXT,
                    item_count      INTEGER DEFAULT 0,
                    sent_at         REAL    NOT NULL,
                    success         INTEGER NOT NULL DEFAULT 0,
                    error           TEXT    DEFAULT '',
                    sources         TEXT    DEFAULT '[]'
                )
            """)
            c.execute("CREATE INDEX IF NOT EXISTS idx_hist_sent ON delivery_history(sent_at DESC)")
            c.commit()
            return
        with c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS subscriptions (
                    id           TEXT PRIMARY KEY,
                    theme        TEXT NOT NULL,
                    email        TEXT NOT NULL,
                    frequency    TEXT NOT NULL DEFAULT 'daily',
                    count        INTEGER NOT NULL DEFAULT 5,
                    active       INTEGER NOT NULL DEFAULT 1,
                    created_at   REAL    NOT NULL,
                    last_sent_at REAL,
                    tags         TEXT    NOT NULL DEFAULT '[]'
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS delivery_history (
                    id              TEXT PRIMARY KEY,
                    subscription_id TEXT,
                    theme           TEXT,
                    email           TEXT,
                    subject         TEXT,
                    item_count      INTEGER DEFAULT 0,
                    sent_at         REAL    NOT NULL,
                    success         INTEGER NOT NULL DEFAULT 0,
                    error           TEXT    DEFAULT '',
                    sources         TEXT    DEFAULT '[]'
                )
            """)
            c.execute("CREATE INDEX IF NOT EXISTS idx_hist_sent ON delivery_history(sent_at DESC)")

    # ── Subscription CRUD ────────────────────────────────────────────

    def create(
        self,
        theme: str,
        email: str,
        frequency: str = "daily",
        count: int = 5,
        tags: Optional[List[str]] = None,
    ) -> Subscription:
        sub = Subscription(
            id=uuid.uuid4().hex[:12],
            theme=theme, email=email, frequency=frequency, count=count,
            created_at=time.time(), tags=tags or [],
        )
        with self._conn() as c:
            c.execute(
                "INSERT INTO subscriptions VALUES (?,?,?,?,?,?,?,?,?)",
                (sub.id, sub.theme, sub.email, sub.frequency, sub.count,
                 1, sub.created_at, None, json.dumps(sub.tags)),
            )
        return sub

    def list_all(self) -> List[Subscription]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM subscriptions ORDER BY created_at DESC"
            ).fetchall()
        return [self._row_to_sub(r) for r in rows]

    def get(self, sub_id: str) -> Optional[Subscription]:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM subscriptions WHERE id=?", (sub_id,)
            ).fetchone()
        return self._row_to_sub(row) if row else None

    def update(self, sub_id: str, **fields) -> Optional[Subscription]:
        if not fields:
            return self.get(sub_id)
        # tags は JSON シリアライズ
        if "tags" in fields:
            fields["tags"] = json.dumps(fields["tags"])
        set_clause = ", ".join(f"{k}=?" for k in fields)
        values = list(fields.values()) + [sub_id]
        with self._conn() as c:
            c.execute(f"UPDATE subscriptions SET {set_clause} WHERE id=?", values)
        return self.get(sub_id)

    def delete(self, sub_id: str) -> bool:
        with self._conn() as c:
            cur = c.execute("DELETE FROM subscriptions WHERE id=?", (sub_id,))
        return cur.rowcount > 0

    def bulk_update(self, ids: List[str], **fields) -> int:
        """複数の購読を一括更新する。更新件数を返す。"""
        if not ids or not fields:
            return 0
        if "tags" in fields:
            fields["tags"] = json.dumps(fields["tags"])
        set_clause = ", ".join(f"{k}=?" for k in fields)
        placeholders = ",".join("?" * len(ids))
        values = list(fields.values()) + ids
        with self._conn() as c:
            cur = c.execute(
                f"UPDATE subscriptions SET {set_clause} WHERE id IN ({placeholders})", values
            )
        return cur.rowcount

    def bulk_delete(self, ids: List[str]) -> int:
        """複数の購読を一括削除する。削除件数を返す。"""
        if not ids:
            return 0
        placeholders = ",".join("?" * len(ids))
        with self._conn() as c:
            cur = c.execute(f"DELETE FROM subscriptions WHERE id IN ({placeholders})", ids)
        return cur.rowcount

    def due_subscriptions(self) -> List[Subscription]:
        now = time.time()
        return [
            s for s in self.list_all()
            if s.active and (
                s.last_sent_at is None
                or (now - s.last_sent_at) >= _INTERVALS.get(s.frequency, 86400)
            )
        ]

    def export_json(self) -> str:
        return json.dumps([s.to_dict() for s in self.list_all()], ensure_ascii=False, indent=2)

    def import_json(self, data: str) -> int:
        """JSON 文字列から購読を一括インポートする。追加件数を返す。"""
        items = json.loads(data)
        count = 0
        for item in items:
            if "theme" in item and "email" in item:
                self.create(
                    theme=item["theme"],
                    email=item["email"],
                    frequency=item.get("frequency", "daily"),
                    count=item.get("count", 5),
                    tags=item.get("tags", []),
                )
                count += 1
        return count

    def _row_to_sub(self, row: sqlite3.Row) -> Subscription:
        return Subscription(
            id=row["id"], theme=row["theme"], email=row["email"],
            frequency=row["frequency"], count=row["count"],
            active=bool(row["active"]), created_at=row["created_at"],
            last_sent_at=row["last_sent_at"],
            tags=json.loads(row["tags"] or "[]"),
        )

    # ── Delivery History ─────────────────────────────────────────────

    def add_history(self, record: DeliveryRecord) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO delivery_history VALUES (?,?,?,?,?,?,?,?,?,?)",
                (record.id, record.subscription_id, record.theme, record.email,
                 record.subject, record.item_count, record.sent_at,
                 1 if record.success else 0, record.error,
                 json.dumps(record.sources)),
            )

    def list_history(self, limit: int = 200, theme: str = "") -> List[DeliveryRecord]:
        with self._conn() as c:
            if theme:
                rows = c.execute(
                    "SELECT * FROM delivery_history WHERE theme=? ORDER BY sent_at DESC LIMIT ?",
                    (theme, limit),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM delivery_history ORDER BY sent_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [self._row_to_hist(r) for r in rows]

    def history_stats(self) -> dict:
        with self._conn() as c:
            total  = c.execute("SELECT COUNT(*) FROM delivery_history WHERE success=1").fetchone()[0]
            arts   = c.execute("SELECT SUM(item_count) FROM delivery_history WHERE success=1").fetchone()[0] or 0
            themes = c.execute("SELECT COUNT(DISTINCT theme) FROM delivery_history").fetchone()[0]
            last   = c.execute("SELECT MAX(sent_at) FROM delivery_history WHERE success=1").fetchone()[0]
        return {
            "total_deliveries": total,
            "total_articles":   arts,
            "unique_themes":    themes,
            "last_sent_at":     last,
        }

    def _row_to_hist(self, row: sqlite3.Row) -> DeliveryRecord:
        return DeliveryRecord(
            id=row["id"], subscription_id=row["subscription_id"],
            theme=row["theme"], email=row["email"], subject=row["subject"],
            item_count=row["item_count"], sent_at=row["sent_at"],
            success=bool(row["success"]), error=row["error"] or "",
            sources=json.loads(row["sources"] or "[]"),
        )
