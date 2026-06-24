"""FastAPI ルーター + ダッシュボード HTML。"""
from __future__ import annotations

import concurrent.futures
import re
import socket
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from .auth import (
    SESSION_COOKIE,
    SESSION_EXPIRES,
    get_session_email,
    is_admin,
    magic_link_email_body,
    magic_link_url,
    make_magic_token,
    make_session_token,
    verify_magic_token,
)
from .models import DeliveryFrequency
from .summarizer import DEFAULT_MAX_CHARS

router = APIRouter()

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Pydantic モデル
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class SubscriptionCreate(BaseModel):
    theme:     str = Field(..., description="関心テーマ")
    email:     str = Field(..., description="配信先メール")
    frequency: str = Field("daily")
    count:     int = Field(5, ge=1, le=20)
    tags:      List[str] = Field(default_factory=list)

class SubscriptionUpdate(BaseModel):
    theme:     Optional[str]  = None
    email:     Optional[str]  = None
    frequency: Optional[str]  = None
    count:     Optional[int]  = Field(None, ge=1, le=20)
    active:    Optional[bool] = None
    tags:      Optional[List[str]] = None

class BulkAction(BaseModel):
    ids:    List[str]       = Field(..., description="対象 ID リスト")
    action: str             = Field(..., description="pause | resume | delete")

class ImportRequest(BaseModel):
    data: str = Field(..., description="JSON 文字列")

class PreviewRequest(BaseModel):
    theme: str = Field(...)
    count: int = Field(5, ge=1, le=20)

class EmailValidateRequest(BaseModel):
    email: str

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Email バリデーション
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")


def _check_email(email: str) -> Optional[str]:
    email = email.strip()
    if not _EMAIL_RE.match(email):
        return "メールアドレスの形式が正しくありません"
    domain = email.split("@", 1)[1].lower()

    try:
        import dns.resolver  # type: ignore
        import dns.exception  # type: ignore
        try:
            dns.resolver.resolve(domain, "MX", lifetime=5)
            return None
        except dns.resolver.NXDOMAIN:
            return f"ドメイン「{domain}」は存在しません"
        except dns.resolver.NoAnswer:
            try:
                dns.resolver.resolve(domain, "A", lifetime=5)
                return None
            except dns.resolver.NXDOMAIN:
                return f"ドメイン「{domain}」は存在しません"
            except Exception:
                pass
        except (dns.exception.Timeout, Exception):
            return None
    except ImportError:
        pass

    def _resolve() -> bool:
        try:
            socket.getaddrinfo(domain, None)
            return True
        except socket.gaierror:
            return False

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            ok = ex.submit(_resolve).result(timeout=5)
        return None if ok else f"ドメイン「{domain}」は存在しません"
    except Exception:
        return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ヘルパー: store / engine をリクエストから取得
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _store(req: Request):
    return req.app.state.store

def _engine(req: Request):
    return req.app.state.engine


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 購読 CRUD
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _require_login(req: Request) -> str:
    """セッションからメールアドレスを取得する。未ログインなら 401。"""
    email = get_session_email(req)
    if not email:
        raise HTTPException(401, "ログインが必要です。/login からログインしてください。")
    return email


@router.post("/v1/subscriptions", tags=["subscriptions"])
def create_subscription(req: Request, body: SubscriptionCreate):
    owner = _require_login(req)
    try:
        DeliveryFrequency.from_str(body.frequency)
    except ValueError as e:
        raise HTTPException(422, str(e))
    err = _check_email(body.email)
    if err:
        raise HTTPException(422, err)
    sub = _store(req).create(
        theme=body.theme, email=body.email,
        frequency=body.frequency, count=body.count, tags=body.tags,
        owner_email=owner,
    )
    return sub.to_dict()


@router.get("/v1/subscriptions", tags=["subscriptions"])
def list_subscriptions(req: Request):
    owner = _require_login(req)
    # 管理者は全件、一般ユーザーは自分の購読のみ
    if is_admin(owner):
        subs = _store(req).list_all()
    else:
        subs = _store(req).list_all(owner=owner)
    return [s.to_dict() for s in subs]


@router.get("/v1/subscriptions/{sub_id}", tags=["subscriptions"])
def get_subscription(req: Request, sub_id: str):
    owner = _require_login(req)
    sub = _store(req).get(sub_id)
    if not sub:
        raise HTTPException(404, f"購読が見つかりません: {sub_id}")
    if not is_admin(owner) and sub.owner_email != owner:
        raise HTTPException(403, "この購読へのアクセス権がありません")
    return sub.to_dict()


@router.patch("/v1/subscriptions/{sub_id}", tags=["subscriptions"])
def update_subscription(req: Request, sub_id: str, body: SubscriptionUpdate):
    owner = _require_login(req)
    sub = _store(req).get(sub_id)
    if not sub:
        raise HTTPException(404, f"購読が見つかりません: {sub_id}")
    if not is_admin(owner) and sub.owner_email != owner:
        raise HTTPException(403, "この購読を編集する権限がありません")
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if "frequency" in fields:
        try:
            DeliveryFrequency.from_str(fields["frequency"])
        except ValueError as e:
            raise HTTPException(422, str(e))
    if "email" in fields:
        err = _check_email(fields["email"])
        if err:
            raise HTTPException(422, err)
    sub = _store(req).update(sub_id, **fields)
    return sub.to_dict()


@router.delete("/v1/subscriptions/{sub_id}", tags=["subscriptions"])
def delete_subscription(req: Request, sub_id: str):
    owner = _require_login(req)
    sub = _store(req).get(sub_id)
    if not sub:
        raise HTTPException(404, f"購読が見つかりません: {sub_id}")
    if not is_admin(owner) and sub.owner_email != owner:
        raise HTTPException(403, "この購読を削除する権限がありません")
    _store(req).delete(sub_id)
    return {"deleted": True, "id": sub_id}


# ── 一括操作 ─────────────────────────────────────────────────────

@router.post("/v1/subscriptions/bulk", tags=["subscriptions"])
def bulk_action(req: Request, body: BulkAction):
    owner = _require_login(req)
    store = _store(req)
    # 管理者以外は自分の購読のみ対象
    if not is_admin(owner):
        owned = {s.id for s in store.list_all(owner=owner)}
        body.ids = [i for i in body.ids if i in owned]
    if body.action == "pause":
        n = store.bulk_update(body.ids, active=0)
        return {"action": "pause", "updated": n}
    if body.action == "resume":
        n = store.bulk_update(body.ids, active=1)
        return {"action": "resume", "updated": n}
    if body.action == "delete":
        n = store.bulk_delete(body.ids)
        return {"action": "delete", "deleted": n}
    raise HTTPException(422, f"不明なアクション: {body.action}")


# ── インポート / エクスポート ────────────────────────────────────

@router.get("/v1/subscriptions/export/json", tags=["subscriptions"])
def export_subscriptions(req: Request):
    owner = _require_login(req)
    subs = _store(req).list_all() if is_admin(owner) else _store(req).list_all(owner=owner)
    import json as _json
    return {"data": _json.dumps([s.to_dict() for s in subs], ensure_ascii=False, indent=2)}


@router.post("/v1/subscriptions/import/json", tags=["subscriptions"])
def import_subscriptions(req: Request, body: ImportRequest):
    owner = _require_login(req)
    try:
        import json as _json
        items = _json.loads(body.data)
        count = 0
        for item in items:
            if "theme" in item and "email" in item:
                _store(req).create(
                    theme=item["theme"], email=item["email"],
                    frequency=item.get("frequency", "daily"),
                    count=item.get("count", 5), tags=item.get("tags", []),
                    owner_email=owner,
                )
                count += 1
        return {"imported": count}
    except Exception as e:
        raise HTTPException(400, f"インポートに失敗しました: {e}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# プレビュー / 配信
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@router.post("/v1/preview", tags=["delivery"])
def preview_digest(req: Request, body: PreviewRequest):
    from .models import Subscription
    dummy = Subscription(
        id="preview", theme=body.theme,
        email="preview@example.com",
        frequency="daily", count=body.count,
    )
    engine = _engine(req)
    items = engine.build_items(dummy)
    subject, email_body = engine.format_email(dummy, items)
    sources = list(dict.fromkeys(it.media for it in items if it.media))
    return {
        "subject": subject,
        "body":    email_body,
        "items":   [it.to_dict() for it in items],
        "sources": sources,
        "summarizer": engine.summarizer.provider,
    }


@router.post("/v1/subscriptions/{sub_id}/send", tags=["delivery"])
def send_now(req: Request, sub_id: str):
    sub = _store(req).get(sub_id)
    if not sub:
        raise HTTPException(404, f"購読が見つかりません: {sub_id}")
    return _engine(req).send(sub)


@router.post("/v1/scheduler/run", tags=["delivery"])
def scheduler_run(req: Request):
    """Cloud Scheduler から定期呼び出し。配信待ち購読をすべて処理する。"""
    results = _engine(req).run_due()
    sent  = sum(1 for r in results if r["sent"])
    total = len(results)
    return {"processed": total, "sent": sent, "results": results}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 配信履歴
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@router.get("/v1/history", tags=["history"])
def list_history(req: Request, limit: int = 200, theme: str = ""):
    records = _store(req).list_history(limit=limit, theme=theme)
    return [r.to_dict() for r in records]


@router.get("/v1/history/stats", tags=["history"])
def history_stats(req: Request):
    return _store(req).history_stats()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Email バリデーション
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@router.post("/v1/validate-email", tags=["util"])
def validate_email(body: EmailValidateRequest):
    err = _check_email(body.email)
    if err:
        raise HTTPException(422, err)
    return {"valid": True, "email": body.email}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ダッシュボード HTML
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 認証ルート（マジックリンク）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@router.get("/login", response_class=HTMLResponse, include_in_schema=False)
def login_page(req: Request, sent: str = "", error: str = ""):
    email = get_session_email(req)
    if email:
        return RedirectResponse("/dashboard", status_code=303)
    return HTMLResponse(_build_login_html(sent=sent, error=error))


@router.post("/login", include_in_schema=False)
async def login_post(req: Request):
    form = await req.form()
    email = str(form.get("email", "")).strip().lower()
    err = _check_email(email)
    if err:
        return HTMLResponse(_build_login_html(sent="", error=err))

    token = make_magic_token(email)
    link  = magic_link_url(req, token)
    subject, body = magic_link_email_body(link)

    mailer = req.app.state.mailer
    if mailer.is_configured:
        try:
            mailer.send(email, subject, body)
        except Exception as exc:
            return HTMLResponse(_build_login_html(
                sent="",
                error=f"メール送信に失敗しました: {exc}",
            ))
    else:
        # 開発環境: リンクをログに出力（コンソール確認用）
        import logging
        logging.getLogger(__name__).warning("MAGIC LINK (SMTP未設定): %s", link)

    return RedirectResponse(f"/login?sent=1", status_code=303)


@router.get("/auth/verify", include_in_schema=False)
def auth_verify(req: Request, token: str = ""):
    email = verify_magic_token(token)
    if not email:
        return RedirectResponse("/login?error=expired", status_code=303)

    session_token = make_session_token(email)
    response = RedirectResponse("/dashboard", status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        session_token,
        max_age=SESSION_EXPIRES,
        httponly=True,
        samesite="lax",
        secure=str(req.url).startswith("https"),
    )
    return response


@router.get("/logout", include_in_schema=False)
def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ダッシュボード HTML
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@router.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
def dashboard(req: Request):
    email = get_session_email(req)
    if not email:
        return RedirectResponse("/login", status_code=303)

    admin_flag = is_admin(email)
    admin_badge = (
        '<span style="background:var(--brand);color:#fff;'
        'font-size:.7rem;padding:.15rem .55rem;border-radius:5px;'
        'margin-right:.4rem;font-weight:600;">ADMIN</span>'
        if admin_flag else ""
    )

    freq_opts = "".join(
        f'<option value="{f.value}">{f.value}</option>'
        for f in DeliveryFrequency
    )
    freq_opts_all = '<option value="">すべて</option>' + freq_opts

    return HTMLResponse(_DASHBOARD_HTML.format(
        freq_opts=freq_opts,
        freq_opts_all=freq_opts_all,
        max_chars=DEFAULT_MAX_CHARS,
        user_email=email,
        admin_badge=admin_badge,
    ))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# HTML テンプレート（f-string ではなく .format() を使用）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_DASHBOARD_HTML = """\
<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>News Digest — ダッシュボード</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"></script>
<style>
/* ── Design System ───────────────────────────────────────────── */
:root {{
  --bg:       #080d1a;
  --s1:       #0d1526;
  --s2:       #131f35;
  --s3:       #1a2744;
  --border:   rgba(255,255,255,.07);
  --b2:       rgba(255,255,255,.12);
  --text:     #f1f5f9;
  --t2:       #cbd5e1;
  --muted:    #64748b;
  --brand:    #6366f1;
  --blight:   #818cf8;
  --bglow:    rgba(99,102,241,.18);
  --green:    #10b981;
  --yellow:   #f59e0b;
  --red:      #ef4444;
  --cyan:     #06b6d4;
  --r:        14px;
  --rsm:      9px;
}}
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0;}}
html{{scroll-behavior:smooth;}}
body{{background:var(--bg);color:var(--text);font-family:'Inter','Segoe UI',system-ui,sans-serif;font-size:14px;line-height:1.6;}}
a{{color:var(--blight);text-decoration:none;}}
a:hover{{text-decoration:underline;}}
::-webkit-scrollbar{{width:5px;height:5px;}}
::-webkit-scrollbar-track{{background:transparent;}}
::-webkit-scrollbar-thumb{{background:var(--s3);border-radius:9999px;}}

/* ── Header ───────────────────────────────────────────────────── */
.hdr{{position:sticky;top:0;z-index:200;height:56px;
  background:rgba(8,13,26,.9);backdrop-filter:blur(16px);
  border-bottom:1px solid var(--border);
  display:flex;align-items:center;justify-content:space-between;padding:0 1.5rem;}}
.logo{{display:flex;align-items:center;gap:.6rem;font-size:1.05rem;font-weight:800;}}
.logo-icon{{width:32px;height:32px;border-radius:8px;
  background:linear-gradient(135deg,var(--brand),var(--cyan));
  display:flex;align-items:center;justify-content:center;font-size:1rem;}}
.hdr-right{{display:flex;align-items:center;gap:.75rem;font-size:.76rem;}}
.sdot{{width:8px;height:8px;border-radius:50%;background:var(--green);
  box-shadow:0 0 8px rgba(16,185,129,.6);
  animation:pulse-dot 2.5s ease-in-out infinite;}}
@keyframes pulse-dot{{0%,100%{{box-shadow:0 0 0 0 rgba(16,185,129,.4);}}50%{{box-shadow:0 0 0 5px rgba(16,185,129,0);}}}}
.hdr-div{{width:1px;height:18px;background:var(--border);}}

/* ── Hero ─────────────────────────────────────────────────────── */
.hero{{
  background:radial-gradient(ellipse 80% 60% at 50% -10%,rgba(99,102,241,.15) 0%,transparent 70%),
             linear-gradient(180deg,#0d1526 0%,var(--bg) 100%);
  padding:3rem 1.5rem 2.5rem;text-align:center;border-bottom:1px solid var(--border);}}
.eyebrow{{display:inline-flex;align-items:center;gap:.4rem;
  background:rgba(99,102,241,.1);border:1px solid rgba(99,102,241,.25);
  border-radius:9999px;padding:.25rem .9rem;
  font-size:.72rem;font-weight:600;color:var(--blight);
  margin-bottom:1rem;letter-spacing:.04em;text-transform:uppercase;}}
.hero h1{{font-size:clamp(1.8rem,4vw,2.8rem);font-weight:800;
  background:linear-gradient(135deg,#a78bfa,#38bdf8,#34d399);
  background-clip:text;-webkit-background-clip:text;color:transparent;margin-bottom:.7rem;}}
.hero-sub{{color:var(--muted);max-width:540px;margin:0 auto 1.4rem;}}
.pills{{display:flex;gap:.5rem;justify-content:center;flex-wrap:wrap;}}
.pill{{background:var(--s2);border:1px solid var(--border);
  border-radius:9999px;padding:.28rem .9rem;font-size:.74rem;color:var(--t2);}}

/* ── Stats ────────────────────────────────────────────────────── */
.stats{{display:grid;grid-template-columns:repeat(4,1fr);gap:1rem;
  padding:1.5rem;max-width:1320px;margin:0 auto;}}
.sc{{background:var(--s1);border:1px solid var(--border);border-radius:var(--r);
  padding:1.1rem 1.3rem;display:flex;align-items:center;gap:.9rem;
  transition:transform .2s,border-color .2s;}}
.sc:hover{{transform:translateY(-2px);border-color:var(--b2);}}
.si{{width:42px;height:42px;border-radius:9px;flex-shrink:0;
  display:flex;align-items:center;justify-content:center;font-size:1.1rem;}}
.si-b{{background:rgba(99,102,241,.15);}}
.si-g{{background:rgba(16,185,129,.15);}}
.si-y{{background:rgba(245,158,11,.15);}}
.si-c{{background:rgba(6,182,212,.15);}}
.sn{{font-size:1.8rem;font-weight:800;line-height:1;font-variant-numeric:tabular-nums;}}
.sn-b{{color:var(--blight);}} .sn-g{{color:var(--green);}}
.sn-y{{color:var(--yellow);}} .sn-c{{color:var(--cyan);}}
.sl{{font-size:.72rem;color:var(--muted);margin-top:.15rem;}}

/* ── Tabs ─────────────────────────────────────────────────────── */
.main{{max-width:1320px;margin:0 auto;padding:0 1.5rem 3rem;}}
.tabs{{display:flex;gap:0;border-bottom:1px solid var(--border);margin-bottom:1.5rem;}}
.tab{{padding:.7rem 1.2rem;font-size:.88rem;font-weight:600;cursor:pointer;
  color:var(--muted);border-bottom:2px solid transparent;transition:all .15s;}}
.tab:hover{{color:var(--t2);}}
.tab.active{{color:var(--blight);border-bottom-color:var(--brand);}}
.tab-panel{{display:none;}}
.tab-panel.active{{display:block;}}

/* ── Card ─────────────────────────────────────────────────────── */
.card{{background:var(--s1);border:1px solid var(--border);
  border-radius:var(--r);padding:1.4rem;transition:border-color .2s;}}
.card:hover{{border-color:var(--b2);}}
.card-accent{{
  border-top:2px solid transparent;
  background-image:linear-gradient(var(--s1),var(--s1)),linear-gradient(90deg,var(--brand),var(--cyan));
  background-origin:border-box;background-clip:padding-box,border-box;}}
.ch{{display:flex;align-items:center;justify-content:space-between;margin-bottom:1rem;}}
.ct{{font-size:.92rem;font-weight:700;color:#e2e8f0;display:flex;align-items:center;gap:.5rem;}}
.ctbar{{width:3px;height:1em;border-radius:9999px;flex-shrink:0;
  background:linear-gradient(180deg,var(--brand),var(--cyan));}}
.ca{{display:flex;gap:.4rem;align-items:center;}}
.grid2{{display:grid;grid-template-columns:1fr 1fr;gap:1.2rem;margin-bottom:1.2rem;}}
.grid3{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:1rem;margin-bottom:1rem;}}
.mb{{margin-bottom:1.2rem;}}

/* ── Form ─────────────────────────────────────────────────────── */
label.fl{{display:block;margin:.75rem 0 .28rem;
  font-size:.72rem;font-weight:600;color:var(--muted);
  letter-spacing:.04em;text-transform:uppercase;}}
input,select{{width:100%;padding:.56rem .8rem;font-size:.9rem;font-family:inherit;
  background:var(--s3);color:var(--text);
  border:1px solid rgba(255,255,255,.08);border-radius:var(--rsm);outline:none;
  transition:border-color .15s,box-shadow .15s;}}
input::placeholder{{color:var(--muted);}}
input:focus,select:focus{{border-color:rgba(99,102,241,.6);box-shadow:0 0 0 3px var(--bglow);}}
input.invalid{{border-color:var(--red)!important;box-shadow:0 0 0 3px rgba(239,68,68,.15)!important;}}
.ferr{{color:#f87171;font-size:.74rem;margin-top:.28rem;min-height:.9rem;}}
.frow{{display:flex;gap:.7rem;margin-top:.7rem;}}
.fcol{{flex:1;}}
.brow{{display:flex;gap:.55rem;margin-top:1.1rem;}}

/* ── Buttons ──────────────────────────────────────────────────── */
.btn{{display:inline-flex;align-items:center;justify-content:center;gap:.4rem;
  padding:.55rem 1rem;font-size:.86rem;font-family:inherit;font-weight:600;
  border:0;border-radius:var(--rsm);cursor:pointer;transition:all .15s;flex:1;white-space:nowrap;}}
.btn:active:not(:disabled){{transform:scale(.97);}}
.btn:disabled{{opacity:.4;cursor:not-allowed;transform:none!important;}}
.bp{{background:linear-gradient(135deg,var(--brand),#4f46e5);color:#fff;box-shadow:0 2px 12px rgba(99,102,241,.4);}}
.bp:hover:not(:disabled){{box-shadow:0 4px 20px rgba(99,102,241,.55);}}
.bo{{background:transparent;color:var(--blight);border:1px solid rgba(99,102,241,.45);}}
.bo:hover:not(:disabled){{background:rgba(99,102,241,.08);}}
.bg{{background:var(--s2);color:var(--t2);border:1px solid var(--border);}}
.bg:hover:not(:disabled){{background:var(--s3);}}
.bsuc{{background:rgba(16,185,129,.12);color:var(--green);border:1px solid rgba(16,185,129,.3);}}
.bsuc:hover:not(:disabled){{background:rgba(16,185,129,.22);}}
.bdng{{background:rgba(239,68,68,.1);color:#f87171;border:1px solid rgba(239,68,68,.28);}}
.bdng:hover:not(:disabled){{background:rgba(239,68,68,.2);}}
.bwrn{{background:rgba(245,158,11,.1);color:var(--yellow);border:1px solid rgba(245,158,11,.28);}}
.bwrn:hover:not(:disabled){{background:rgba(245,158,11,.2);}}
.bsm{{padding:.28rem .65rem;font-size:.74rem;flex:unset;border-radius:6px;}}
.bico{{padding:.3rem;width:28px;height:28px;flex:unset;border-radius:6px;}}
.spin{{width:13px;height:13px;border-radius:50%;border:2px solid rgba(255,255,255,.2);
  border-top-color:#fff;animation:sp .7s linear infinite;}}
@keyframes sp{{to{{transform:rotate(360deg);}}}}

/* ── Sub List ─────────────────────────────────────────────────── */
.bulk-bar{{display:flex;align-items:center;gap:.5rem;padding:.6rem .8rem;
  background:rgba(99,102,241,.08);border:1px solid rgba(99,102,241,.2);
  border-radius:var(--rsm);margin-bottom:.8rem;font-size:.8rem;}}
.bulk-bar select{{width:auto;padding:.28rem .5rem;font-size:.76rem;}}
#sub-list{{display:flex;flex-direction:column;gap:.55rem;max-height:460px;overflow-y:auto;padding-right:.2rem;}}
.sub-item{{background:var(--s2);border:1px solid var(--border);border-radius:10px;
  padding:.85rem 1rem;display:flex;align-items:center;gap:.85rem;
  transition:border-color .15s,background .15s;}}
.sub-item:hover{{background:var(--s3);border-color:var(--b2);}}
.sub-item.selected{{border-color:rgba(99,102,241,.5);background:rgba(99,102,241,.05);}}
.sub-cb{{flex-shrink:0;width:16px;height:16px;cursor:pointer;accent-color:var(--brand);}}
.sub-av{{width:36px;height:36px;border-radius:8px;flex-shrink:0;
  background:linear-gradient(135deg,var(--brand),var(--cyan));
  display:flex;align-items:center;justify-content:center;font-size:.9rem;font-weight:700;}}
.sub-bd{{flex:1;min-width:0;}}
.sub-nm{{font-weight:700;font-size:.9rem;display:flex;align-items:center;gap:.4rem;flex-wrap:wrap;}}
.sub-dt{{font-size:.72rem;color:var(--muted);margin-top:.15rem;}}
.sub-ac{{display:flex;gap:.3rem;flex-shrink:0;}}
.dot{{width:7px;height:7px;border-radius:50%;flex-shrink:0;}}
.don{{background:var(--green);box-shadow:0 0 5px rgba(16,185,129,.6);}}
.doff{{background:var(--muted);}}
.fb{{display:inline-block;padding:.1rem .5rem;border-radius:9999px;font-size:.66rem;font-weight:700;}}
.fh{{background:rgba(167,139,250,.15);color:#a78bfa;}}
.fd{{background:rgba(56,189,248,.15);color:#38bdf8;}}
.fw{{background:rgba(52,211,153,.15);color:#34d399;}}
.fm{{background:rgba(251,146,60,.15);color:#fb923c;}}
.due-chip{{background:rgba(245,158,11,.1);color:var(--yellow);
  border:1px solid rgba(245,158,11,.28);border-radius:9999px;
  padding:.1rem .5rem;font-size:.66rem;font-weight:700;}}
#sub-empty{{color:var(--muted);text-align:center;padding:2.5rem 0;
  display:flex;flex-direction:column;align-items:center;gap:.5rem;font-size:.86rem;}}

/* ── Preview ──────────────────────────────────────────────────── */
.preview-hdr{{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:.5rem;margin-bottom:.7rem;}}
.srcs{{display:flex;flex-wrap:wrap;gap:.35rem;align-items:center;min-height:1.5rem;}}
.slbl{{font-size:.72rem;color:var(--muted);font-weight:600;}}
.sbadge{{background:rgba(6,182,212,.1);color:#67e8f9;border:1px solid rgba(6,182,212,.25);
  padding:.12rem .6rem;border-radius:9999px;font-size:.7rem;font-weight:600;}}
.prov-badge{{background:rgba(99,102,241,.1);color:var(--blight);border:1px solid rgba(99,102,241,.2);
  padding:.15rem .6rem;border-radius:9999px;font-size:.7rem;font-weight:600;}}
#prev-list{{display:flex;flex-direction:column;gap:.65rem;max-height:460px;overflow-y:auto;padding-right:.2rem;}}
.ac{{background:var(--s2);border:1px solid var(--border);border-radius:10px;padding:.9rem 1rem;
  transition:border-color .15s,background .15s;}}
.ac:hover{{background:var(--s3);border-color:var(--b2);}}
.ac-meta{{display:flex;align-items:center;justify-content:space-between;margin-bottom:.4rem;}}
.ac-media{{font-size:.68rem;font-weight:700;color:var(--blight);
  background:rgba(99,102,241,.1);border:1px solid rgba(99,102,241,.18);
  padding:.1rem .5rem;border-radius:9999px;}}
.ac-date{{font-size:.68rem;color:var(--muted);}}
.ac-title{{font-weight:700;font-size:.88rem;line-height:1.4;margin-bottom:.3rem;}}
.ac-sum{{font-size:.8rem;color:var(--t2);line-height:1.6;margin-bottom:.45rem;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;}}
.ac-link{{font-size:.72rem;color:var(--blight);font-weight:600;}}
.ac-link:hover{{color:#c7d2fe;text-decoration:none;}}
#prev-ph{{color:var(--muted);text-align:center;padding:3rem 0;
  display:flex;flex-direction:column;align-items:center;gap:.5rem;}}
.pld{{display:flex;align-items:center;justify-content:center;gap:.6rem;
  padding:2rem 0;color:var(--muted);font-size:.86rem;}}

/* ── History ──────────────────────────────────────────────────── */
.hist-stats{{display:grid;grid-template-columns:repeat(4,1fr);gap:.8rem;margin-bottom:1.2rem;}}
.hsc{{background:var(--s2);border:1px solid var(--border);border-radius:10px;
  padding:1rem;text-align:center;}}
.hsc-n{{font-size:1.6rem;font-weight:800;color:var(--blight);}}
.hsc-l{{font-size:.72rem;color:var(--muted);margin-top:.15rem;}}
.hist-filters{{display:flex;gap:.5rem;margin-bottom:.8rem;flex-wrap:wrap;}}
.hist-filters input,
.hist-filters select{{width:auto;flex:1;min-width:120px;padding:.38rem .7rem;font-size:.8rem;}}
#hist-table{{width:100%;border-collapse:collapse;font-size:.8rem;}}
#hist-table th{{text-align:left;padding:.55rem .75rem;border-bottom:1px solid var(--border);
  color:var(--muted);font-weight:600;font-size:.72rem;text-transform:uppercase;letter-spacing:.04em;}}
#hist-table td{{padding:.55rem .75rem;border-bottom:1px solid var(--border);vertical-align:top;}}
#hist-table tr:hover td{{background:var(--s2);}}
.hist-success{{color:var(--green);font-weight:600;}}
.hist-fail{{color:var(--red);font-weight:600;}}
.hist-wrap{{max-height:460px;overflow-y:auto;border-radius:var(--rsm);
  border:1px solid var(--border);}}

/* ── Scheduler Info ────────────────────────────────────────────── */
.sched-info{{background:var(--s2);border:1px solid var(--border);border-radius:10px;
  padding:1.2rem 1.4rem;font-size:.84rem;line-height:1.8;}}
.sched-info code{{background:var(--s3);padding:.2rem .6rem;border-radius:5px;
  font-size:.78rem;color:var(--blight);white-space:pre-wrap;word-break:break-all;}}
.sched-info .cmd-block{{
  background:var(--s3);border:1px solid var(--border);border-radius:8px;
  padding:.9rem 1rem;margin:.5rem 0;font-family:monospace;font-size:.78rem;
  color:#a5b4fc;line-height:1.7;overflow-x:auto;}}
.sched-section{{margin-bottom:1.4rem;}}
.sched-section h3{{font-size:.86rem;font-weight:700;color:var(--t2);margin-bottom:.5rem;}}
.step-list{{list-style:none;counter-reset:step;}}
.step-list li{{counter-increment:step;display:flex;gap:.7rem;margin-bottom:.6rem;}}
.step-list li::before{{
  content:counter(step);
  min-width:22px;height:22px;border-radius:50%;
  background:var(--brand);color:#fff;
  display:flex;align-items:center;justify-content:center;
  font-size:.72rem;font-weight:700;flex-shrink:0;margin-top:.15rem;}}

/* ── Charts ────────────────────────────────────────────────────── */
.cbox{{background:var(--s2);border:1px solid var(--border);border-radius:10px;padding:1.1rem;}}
.ctitle{{font-size:.74rem;font-weight:600;color:var(--muted);
  margin-bottom:.8rem;text-transform:uppercase;letter-spacing:.04em;}}
.mbig{{font-size:2.2rem;font-weight:800;line-height:1;font-variant-numeric:tabular-nums;}}
.msub{{font-size:.7rem;color:var(--muted);margin-top:.3rem;}}

/* ── Toast ────────────────────────────────────────────────────── */
#toast{{position:fixed;bottom:1.5rem;right:1.5rem;
  background:var(--s3);border:1px solid rgba(255,255,255,.1);border-radius:10px;
  padding:.8rem 1.1rem;font-size:.84rem;color:var(--text);
  box-shadow:0 4px 32px rgba(0,0,0,.5);
  transform:translateY(140%) scale(.96);opacity:0;
  transition:transform .28s cubic-bezier(.34,1.56,.64,1),opacity .2s;
  z-index:9999;max-width:340px;
  display:flex;align-items:center;gap:.5rem;}}
#toast.show{{transform:translateY(0) scale(1);opacity:1;}}

/* ── Footer ───────────────────────────────────────────────────── */
footer{{border-top:1px solid var(--border);padding:1rem 1.5rem;
  text-align:center;font-size:.72rem;color:var(--muted);
  display:flex;justify-content:center;gap:1.5rem;flex-wrap:wrap;}}

/* ── Responsive ───────────────────────────────────────────────── */
@media(max-width:960px){{
  .stats{{grid-template-columns:repeat(2,1fr);}}
  .grid2{{grid-template-columns:1fr;}}
  .grid3{{grid-template-columns:1fr 1fr;}}
  .hist-stats{{grid-template-columns:repeat(2,1fr);}}
}}
@media(max-width:600px){{
  .stats{{grid-template-columns:1fr 1fr;}}
  .grid3{{grid-template-columns:1fr;}}
  .hist-stats{{grid-template-columns:1fr 1fr;}}
}}
</style>
</head>
<body>

<!-- Header -->
<header class="hdr">
  <div class="logo"><div class="logo-icon">📰</div>News Digest</div>
  <div class="hdr-right">
    <div class="sdot"></div>
    <span id="h-status" style="color:var(--green);font-weight:600;">稼働中</span>
    <div class="hdr-div"></div>
    <span id="h-smtp" style="color:var(--muted);">確認中...</span>
    <div class="hdr-div"></div>
    <span id="h-summ" style="color:var(--muted);">要約: —</span>
    <div class="hdr-div"></div>
    {admin_badge}<span style="color:var(--t2);font-size:.76rem;">{user_email}</span>
    <a href="/logout" style="color:var(--muted);font-size:.75rem;padding:.2rem .6rem;border:1px solid var(--border);border-radius:6px;margin-left:.4rem;">ログアウト</a>
  </div>
</header>

<!-- Hero -->
<section class="hero">
  <div class="eyebrow">✦ News Intelligence Platform</div>
  <h1>最新ニュースを、自動で。</h1>
  <p class="hero-sub">テーマを登録するだけ。日本の主要メディアから {max_chars} 字の要約付きで自動配信。</p>
  <div class="pills">
    <span class="pill">🔍 日本優先検索 (jp-jp)</span>
    <span class="pill">🤖 LLM / ルールベース要約</span>
    <span class="pill">📧 SMTP 自動配信</span>
    <span class="pill">⏰ Cloud Scheduler 対応</span>
    <span class="pill">📜 配信履歴 (SQLite)</span>
  </div>
</section>

<!-- Stats -->
<div class="stats">
  <div class="sc"><div class="si si-b">📋</div><div><div class="sn sn-b" id="st-total">—</div><div class="sl">登録件数</div></div></div>
  <div class="sc"><div class="si si-g">✅</div><div><div class="sn sn-g" id="st-active">—</div><div class="sl">アクティブ</div></div></div>
  <div class="sc"><div class="si si-y">⏳</div><div><div class="sn sn-y" id="st-due">—</div><div class="sl">配信待ち</div></div></div>
  <div class="sc"><div class="si si-c">📜</div><div><div class="sn sn-c" id="st-hist">—</div><div class="sl">累計配信数</div></div></div>
</div>

<!-- Main -->
<div class="main">

  <!-- Tabs -->
  <div class="tabs">
    <div class="tab active" data-tab="subscriptions">📋 購読管理</div>
    <div class="tab" data-tab="preview">👁 プレビュー</div>
    <div class="tab" data-tab="history">📜 配信履歴</div>
    <div class="tab" data-tab="scheduler">⏰ スケジューラ</div>
  </div>

  <!-- ═══ TAB: 購読管理 ═════════════════════════════════════════ -->
  <div class="tab-panel active" id="tab-subscriptions">
    <div class="grid2 mb">

      <!-- フォーム -->
      <div class="card card-accent">
        <div class="ch">
          <div class="ct"><span class="ctbar"></span>新規テーマ登録</div>
        </div>
        <label class="fl">テーマ（関心ワード）</label>
        <input id="f-theme" type="text" placeholder="例: 生成AI　量子コンピュータ　自動運転">
        <label class="fl">配信先メールアドレス</label>
        <input id="f-email" type="email" placeholder="you@example.com" autocomplete="email">
        <div id="email-err" class="ferr"></div>
        <div class="frow">
          <div class="fcol">
            <label class="fl">配信頻度</label>
            <select id="f-freq">{freq_opts}</select>
          </div>
          <div style="width:90px">
            <label class="fl">配信本数</label>
            <input id="f-count" type="number" value="5" min="1" max="20">
          </div>
        </div>
        <label class="fl">タグ（任意・カンマ区切り）</label>
        <input id="f-tags" type="text" placeholder="AI, テクノロジー, ...">
        <div class="brow">
          <button class="btn bo" id="btn-preview-quick">👁 プレビュー</button>
          <button class="btn bp" id="btn-subscribe"><span id="sub-lbl">＋ 登録する</span></button>
        </div>
      </div>

      <!-- 一覧 -->
      <div class="card">
        <div class="ch">
          <div class="ct"><span class="ctbar"></span>購読一覧</div>
          <div class="ca">
            <select id="filter-freq" style="width:auto;padding:.3rem .55rem;font-size:.74rem;">{freq_opts_all}</select>
            <button class="btn bg bsm" onclick="exportSubs()" title="JSON エクスポート">⬇ Export</button>
            <button class="btn bg bsm" onclick="document.getElementById('import-file').click()" title="JSON インポート">⬆ Import</button>
            <input id="import-file" type="file" accept=".json" style="display:none" onchange="importSubs(event)">
            <button class="btn bwrn bsm" onclick="schedulerRun()">⚡ 配信実行</button>
            <button class="btn bg bico" onclick="loadSubs()" title="更新">↺</button>
          </div>
        </div>

        <!-- 一括操作バー -->
        <div class="bulk-bar" id="bulk-bar" style="display:none">
          <span id="bulk-count" style="font-weight:700;color:var(--blight);">0件選択</span>
          <button class="btn bg bsm" onclick="bulkAction('resume')">▶ 再開</button>
          <button class="btn bwrn bsm" onclick="bulkAction('pause')">⏸ 停止</button>
          <button class="btn bdng bsm" onclick="bulkAction('delete')">✕ 削除</button>
          <button class="btn bg bsm" onclick="clearSelection()">選択解除</button>
        </div>

        <div id="sub-list">
          <div id="sub-empty"><span style="font-size:2rem">📭</span>購読がまだありません</div>
        </div>
      </div>
    </div>

    <!-- Charts -->
    <div class="grid2 mb">
      <div class="card">
        <div class="ch"><div class="ct"><span class="ctbar"></span>直近7日間 配信実績</div></div>
        <div class="cbox"><canvas id="freqChart" height="150"></canvas></div>
      </div>
      <div class="card">
        <div class="ch"><div class="ct"><span class="ctbar"></span>テーマ別 収集強度</div></div>
        <div class="cbox"><canvas id="intensityChart" height="150"></canvas></div>
      </div>
    </div>
  </div>

  <!-- ═══ TAB: プレビュー ═══════════════════════════════════════ -->
  <div class="tab-panel" id="tab-preview">
    <div class="card mb">
      <div class="ch">
        <div class="ct"><span class="ctbar"></span>ダイジェスト プレビュー</div>
      </div>
      <p style="font-size:.82rem;color:var(--muted);margin-bottom:.8rem;">
        登録前に「どんな記事が・どの情報源から届くか」を確認できます。
      </p>
      <div class="frow" style="margin-top:0">
        <div class="fcol">
          <input id="pv-theme" type="text" placeholder="テーマを入力（例: 生成AI）">
        </div>
        <div style="width:80px">
          <input id="pv-count" type="number" value="5" min="1" max="10">
        </div>
        <button class="btn bp" style="flex:unset;padding:.56rem 1.2rem" id="btn-preview" onclick="doPreview()">
          <span id="pv-lbl">👁 プレビュー</span>
        </button>
      </div>
      <div class="preview-hdr" style="margin-top:.8rem">
        <div class="srcs" id="prev-srcs"></div>
        <div id="prov-lbl"></div>
      </div>
      <div id="prev-list">
        <div id="prev-ph"><span style="font-size:2rem">🔍</span>テーマを入力して「プレビュー」を押してください</div>
      </div>
    </div>
  </div>

  <!-- ═══ TAB: 配信履歴 ════════════════════════════════════════ -->
  <div class="tab-panel" id="tab-history">
    <div class="card mb">
      <div class="ch">
        <div class="ct"><span class="ctbar"></span>配信履歴</div>
        <div class="ca">
          <button class="btn bg bsm" onclick="loadHistory()">↺ 更新</button>
        </div>
      </div>

      <!-- 集計 -->
      <div class="hist-stats mb" id="hist-stats">
        <div class="hsc"><div class="hsc-n" id="hs-total">—</div><div class="hsc-l">累計配信数</div></div>
        <div class="hsc"><div class="hsc-n" id="hs-arts">—</div><div class="hsc-l">累計記事数</div></div>
        <div class="hsc"><div class="hsc-n" id="hs-themes">—</div><div class="hsc-l">ユニークテーマ数</div></div>
        <div class="hsc"><div class="hsc-n" id="hs-last" style="font-size:1rem">—</div><div class="hsc-l">最終配信</div></div>
      </div>

      <!-- フィルタ -->
      <div class="hist-filters mb">
        <input id="hist-filter-theme" type="text" placeholder="テーマで絞り込み...">
        <select id="hist-filter-status">
          <option value="">すべて</option>
          <option value="success">成功のみ</option>
          <option value="fail">失敗のみ</option>
        </select>
        <button class="btn bg bsm" onclick="applyHistFilter()">絞り込み</button>
      </div>

      <!-- テーブル -->
      <div class="hist-wrap">
        <table id="hist-table">
          <thead>
            <tr>
              <th>日時</th>
              <th>テーマ</th>
              <th>宛先</th>
              <th>記事数</th>
              <th>情報源</th>
              <th>結果</th>
            </tr>
          </thead>
          <tbody id="hist-tbody">
            <tr><td colspan="6" style="text-align:center;color:var(--muted);padding:1.5rem">読み込み中...</td></tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- ═══ TAB: スケジューラ ═══════════════════════════════════ -->
  <div class="tab-panel" id="tab-scheduler">
    <div class="card mb">
      <div class="ch">
        <div class="ct"><span class="ctbar"></span>⏰ Cloud Scheduler 設定ガイド</div>
      </div>

      <div class="sched-section">
        <h3>🔧 仕組み</h3>
        <div class="sched-info">
          Cloud Scheduler が毎朝8時に <code>POST /v1/scheduler/run</code> を呼び出すことで、
          配信タイミングに達した購読を自動処理します。<br>
          認証は Cloud Run のサービスアカウントで行います。
        </div>
      </div>

      <div class="sched-section">
        <h3>📋 セットアップ手順</h3>
        <ol class="step-list">
          <li>
            <div>
              <strong>サービスアカウントを作成</strong>（未作成の場合）
              <div class="cmd-block">gcloud iam service-accounts create news-digest-scheduler \\
  --display-name="News Digest Scheduler" \\
  --project=di-forte-ai-481603</div>
            </div>
          </li>
          <li>
            <div>
              <strong>Cloud Run 呼び出し権限を付与</strong>
              <div class="cmd-block">gcloud run services add-iam-policy-binding news-digest \\
  --region=asia-northeast1 \\
  --member="serviceAccount:news-digest-scheduler@di-forte-ai-481603.iam.gserviceaccount.com" \\
  --role="roles/run.invoker"</div>
            </div>
          </li>
          <li>
            <div>
              <strong>Cloud Scheduler ジョブを作成</strong>（毎朝8時 JST = 23時 UTC）
              <div class="cmd-block">gcloud scheduler jobs create http news-digest-daily \\
  --schedule="0 23 * * *" \\
  --uri="https://news-digest-365979251604.asia-northeast1.run.app/v1/scheduler/run" \\
  --http-method=POST \\
  --oidc-service-account-email=news-digest-scheduler@di-forte-ai-481603.iam.gserviceaccount.com \\
  --location=asia-northeast1 \\
  --time-zone="UTC" \\
  --project=di-forte-ai-481603</div>
            </div>
          </li>
          <li>
            <div>
              <strong>動作確認（手動実行）</strong>
              <div class="cmd-block">gcloud scheduler jobs run news-digest-daily --location=asia-northeast1</div>
            </div>
          </li>
        </ol>
      </div>

      <div class="sched-section">
        <h3>⏰ スケジュール例</h3>
        <div class="sched-info">
          <table style="width:100%;border-collapse:collapse;font-size:.82rem">
            <tr><th style="text-align:left;padding:.4rem .6rem;color:var(--muted)">スケジュール</th><th style="text-align:left;padding:.4rem .6rem;color:var(--muted)">cron 式</th></tr>
            <tr><td style="padding:.4rem .6rem;border-top:1px solid var(--border)">毎朝 8時（JST）</td><td style="padding:.4rem .6rem;border-top:1px solid var(--border)"><code>0 23 * * *</code></td></tr>
            <tr><td style="padding:.4rem .6rem;border-top:1px solid var(--border)">毎時</td><td style="padding:.4rem .6rem;border-top:1px solid var(--border)"><code>0 * * * *</code></td></tr>
            <tr><td style="padding:.4rem .6rem;border-top:1px solid var(--border)">毎週月曜 9時（JST）</td><td style="padding:.4rem .6rem;border-top:1px solid var(--border)"><code>0 0 * * 1</code></td></tr>
          </table>
        </div>
      </div>

      <div class="sched-section">
        <h3>🧪 今すぐ手動実行</h3>
        <div style="display:flex;align-items:center;gap:.8rem;flex-wrap:wrap">
          <button class="btn bwrn" style="flex:unset" onclick="schedulerRun()">⚡ 配信待ちをすべて配信する</button>
          <span style="font-size:.78rem;color:var(--muted)">配信タイミングに達した購読をすべて処理します</span>
        </div>
        <div id="sched-result" style="margin-top:.8rem;font-size:.82rem;color:var(--muted)"></div>
      </div>
    </div>
  </div>

</div>

<!-- Footer -->
<footer>
  <span>News Digest v2.0</span>
  <span>DuckDuckGo News · jp-jp region</span>
  <span>SQLite 配信履歴</span>
  <span>© 2026</span>
</footer>

<div id="toast"><span id="ti"></span><span id="tm"></span></div>

<script>
/* ── Utils ────────────────────────────────────────────────────── */
const $ = id => document.getElementById(id);
let freqChart = null, intensChart = null;
let selectedIds = new Set();
let historyData = [];

function toast(msg, ok = true) {{
  $('ti').textContent = ok ? '✅' : '⚠️';
  $('tm').textContent = msg;
  const t = $('toast');
  t.classList.add('show');
  clearTimeout(t._t);
  t._t = setTimeout(() => t.classList.remove('show'), 3000);
}}

function fmtDate(ts) {{
  if (!ts) return '—';
  return new Date(ts * 1000).toLocaleString('ja-JP', {{
    month:'2-digit', day:'2-digit',
    hour:'2-digit', minute:'2-digit'
  }});
}}
function fmtDateISO(iso) {{
  if (!iso) return '';
  try {{ return new Date(iso).toLocaleDateString('ja-JP', {{month:'short', day:'numeric'}}); }} catch {{ return iso.slice(0,10); }}
}}
function relTime(ts) {{
  if (!ts) return '未配信';
  const m = Math.floor((Date.now()/1000 - ts)/60);
  if (m < 1) return 'たった今';
  if (m < 60) return m + '分前';
  if (m < 1440) return Math.floor(m/60) + '時間前';
  return Math.floor(m/1440) + '日前';
}}
function nextTime(sub) {{
  if (!sub.last_sent_at) return '今すぐ配信可能';
  const iv = {{hourly:3600,daily:86400,weekly:604800,monthly:2592000}}[sub.frequency]||86400;
  const r = (sub.last_sent_at + iv) - Date.now()/1000;
  if (r <= 0) return '今すぐ配信可能';
  const h = Math.floor(r/3600), m = Math.floor((r%3600)/60);
  return `次回まで ${{h}}h ${{m}}m`;
}}
function fb(f) {{
  return `<span class="fb f${{f[0]}}">${{f}}</span>`;
}}
function av(theme) {{
  return (theme||'?')[0].toUpperCase();
}}

/* ── Tabs ─────────────────────────────────────────────────────── */
document.querySelectorAll('.tab').forEach(t => {{
  t.addEventListener('click', () => {{
    document.querySelectorAll('.tab,.tab-panel').forEach(el => el.classList.remove('active'));
    t.classList.add('active');
    $('tab-' + t.dataset.tab).classList.add('active');
    if (t.dataset.tab === 'history') loadHistory();
  }});
}});

/* ── Email Validation ─────────────────────────────────────────── */
function setEmailErr(msg) {{
  const inp = $('f-email'), err = $('email-err');
  if (msg) {{ err.innerHTML = `⚠ ${{msg}}`; inp.classList.add('invalid'); }}
  else     {{ err.innerHTML = ''; inp.classList.remove('invalid'); }}
}}
$('f-email').addEventListener('blur', async () => {{
  const email = $('f-email').value.trim();
  if (!email) {{ setEmailErr(''); return; }}
  try {{
    const r = await fetch('/v1/validate-email', {{
      method:'POST', headers:{{'Content-Type':'application/json'}},
      body: JSON.stringify({{email}})
    }});
    if (!r.ok) {{ const d=await r.json(); setEmailErr(d.detail||'無効なアドレスです'); }}
    else setEmailErr('');
  }} catch{{}}
}});
$('f-email').addEventListener('input', () => setEmailErr(''));

/* ── Subscribe ────────────────────────────────────────────────── */
$('btn-subscribe').addEventListener('click', async () => {{
  const theme = $('f-theme').value.trim();
  const email = $('f-email').value.trim();
  if (!theme) {{ toast('テーマを入力してください', false); return; }}
  if (!email) {{ toast('メールアドレスを入力してください', false); return; }}
  $('sub-lbl').innerHTML = '<span class="spin"></span>';
  $('btn-subscribe').disabled = true;
  try {{
    const r = await fetch('/v1/subscriptions', {{
      method:'POST', headers:{{'Content-Type':'application/json'}},
      body: JSON.stringify({{
        theme, email,
        frequency: $('f-freq').value,
        count: Number($('f-count').value),
        tags: $('f-tags').value.split(',').map(s=>s.trim()).filter(Boolean)
      }})
    }});
    const d = await r.json();
    if (!r.ok) {{
      const msg = d.detail||'登録失敗';
      if (msg.includes('メールアドレス')||msg.includes('ドメイン')) {{ setEmailErr(msg); $('f-email').focus(); }}
      else toast(msg, false);
      return;
    }}
    setEmailErr('');
    toast(`「${{theme}}」を登録しました`);
    $('f-theme').value = $('f-tags').value = '';
    loadSubs();
  }} catch {{ toast('通信エラー', false); }}
  finally {{ $('sub-lbl').textContent = '＋ 登録する'; $('btn-subscribe').disabled = false; }}
}});

/* ── Load Subscriptions ───────────────────────────────────────── */
async function loadSubs() {{
  const ff = $('filter-freq').value;
  try {{
    const r   = await fetch('/v1/subscriptions');
    const all = await r.json();
    const subs = ff ? all.filter(s => s.frequency === ff) : all;
    const now = Date.now()/1000;
    const iv  = {{hourly:3600,daily:86400,weekly:604800,monthly:2592000}};
    const isDue = s => !s.last_sent_at || (now - s.last_sent_at) >= (iv[s.frequency]||86400);
    const active = all.filter(s=>s.active);
    const due    = active.filter(isDue);

    $('st-total').textContent  = all.length;
    $('st-active').textContent = active.length;
    $('st-due').textContent    = due.length;

    updateIntensChart(all);

    const list = $('sub-list');
    if (!subs.length) {{
      list.innerHTML = '<div id="sub-empty"><span style="font-size:2rem">📭</span>購読がまだありません</div>';
      return;
    }}
    list.innerHTML = subs.map(s => `
      <div class="sub-item${{selectedIds.has(s.id)?' selected':''}}" id="si-${{s.id}}">
        <input type="checkbox" class="sub-cb" ${{selectedIds.has(s.id)?'checked':''}}
               onchange="toggleSelect('${{s.id}}',this.checked)">
        <div class="sub-av">${{av(s.theme)}}</div>
        <div class="sub-bd">
          <div class="sub-nm">
            <span class="dot ${{s.active?'don':'doff'}}"></span>
            <span style="overflow:hidden;text-overflow:ellipsis;">${{s.theme}}</span>
            ${{fb(s.frequency)}}
            ${{isDue(s)&&s.active?'<span class="due-chip">配信待ち</span>':''}}
            ${{(s.tags||[]).map(t=>`<span style="font-size:.66rem;background:var(--s3);padding:.1rem .4rem;border-radius:4px;color:var(--muted)">${{t}}</span>`).join('')}}
          </div>
          <div class="sub-dt">${{s.email}} · ${{s.count}}本/回 · ${{relTime(s.last_sent_at)}} · ${{nextTime(s)}}</div>
        </div>
        <div class="sub-ac">
          <button class="btn bsuc bsm" onclick="sendNow('${{s.id}}','${{s.theme.replace(/'/g,"\\'")}}')" title="今すぐ送信">▶ 送信</button>
          <button class="btn bg bsm" onclick="toggleActive('${{s.id}}',${{s.active}})">${{s.active?'⏸ 停止':'▶ 再開'}}</button>
          <button class="btn bdng bsm" onclick="deleteSub('${{s.id}}','${{s.theme.replace(/'/g,"\\'")}}')" title="削除">✕</button>
        </div>
      </div>`).join('');
  }} catch {{ toast('一覧取得に失敗', false); }}
}}

/* ── Selection ────────────────────────────────────────────────── */
function toggleSelect(id, checked) {{
  if (checked) selectedIds.add(id); else selectedIds.delete(id);
  const item = $('si-' + id);
  if (item) item.classList.toggle('selected', checked);
  const bar = $('bulk-bar');
  $('bulk-count').textContent = selectedIds.size + '件選択';
  bar.style.display = selectedIds.size > 0 ? 'flex' : 'none';
}}
function clearSelection() {{
  selectedIds.clear();
  document.querySelectorAll('.sub-cb').forEach(cb => cb.checked = false);
  document.querySelectorAll('.sub-item').forEach(el => el.classList.remove('selected'));
  $('bulk-bar').style.display = 'none';
}}

/* ── Bulk Action ──────────────────────────────────────────────── */
async function bulkAction(action) {{
  const ids = [...selectedIds];
  if (!ids.length) {{ toast('選択してください', false); return; }}
  const labels = {{pause:'停止', resume:'再開', delete:'削除'}};
  if (!confirm(`${{ids.length}}件を${{labels[action]||action}}しますか？`)) return;
  try {{
    const r = await fetch('/v1/subscriptions/bulk', {{
      method:'POST', headers:{{'Content-Type':'application/json'}},
      body: JSON.stringify({{ids, action}})
    }});
    const d = await r.json();
    toast(`${{ids.length}}件を${{labels[action]}}しました`);
    clearSelection();
    loadSubs();
  }} catch {{ toast('一括操作に失敗', false); }}
}}

/* ── Send / Toggle / Delete ───────────────────────────────────── */
async function sendNow(id, theme) {{
  if (!confirm(`「${{theme}}」を今すぐ配信しますか？`)) return;
  try {{
    const r = await fetch(`/v1/subscriptions/${{id}}/send`, {{method:'POST',headers:{{'Content-Length':'0'}}}});
    const d = await r.json();
    if (!r.ok) {{ toast(d.detail||'送信失敗', false); return; }}
    toast(d.sent ? `「${{theme}}」を送信しました ✉` : `「${{theme}}」プレビュー (${{d.reason}})`, d.sent);
    loadSubs();
    loadHistStats();
  }} catch {{ toast('通信エラー', false); }}
}}
async function toggleActive(id, current) {{
  try {{
    const r = await fetch(`/v1/subscriptions/${{id}}`, {{
      method:'PATCH', headers:{{'Content-Type':'application/json'}},
      body: JSON.stringify({{active:!current}})
    }});
    if (!r.ok) {{ toast('更新失敗', false); return; }}
    toast(current ? '購読を停止しました' : '購読を再開しました');
    loadSubs();
  }} catch {{ toast('通信エラー', false); }}
}}
async function deleteSub(id, theme) {{
  if (!confirm(`「${{theme}}」を削除しますか？`)) return;
  try {{
    const r = await fetch(`/v1/subscriptions/${{id}}`, {{method:'DELETE'}});
    if (!r.ok) {{ toast('削除失敗', false); return; }}
    toast(`「${{theme}}」を削除しました`);
    loadSubs();
  }} catch {{ toast('通信エラー', false); }}
}}

/* ── Import / Export ──────────────────────────────────────────── */
async function exportSubs() {{
  const r = await fetch('/v1/subscriptions/export/json');
  const d = await r.json();
  const blob = new Blob([d.data], {{type:'application/json'}});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `news-digest-subs-${{new Date().toISOString().slice(0,10)}}.json`;
  a.click();
  toast('エクスポートしました');
}}
async function importSubs(event) {{
  const file = event.target.files[0];
  if (!file) return;
  const text = await file.text();
  try {{
    const r = await fetch('/v1/subscriptions/import/json', {{
      method:'POST', headers:{{'Content-Type':'application/json'}},
      body: JSON.stringify({{data: text}})
    }});
    const d = await r.json();
    toast(`${{d.imported}}件をインポートしました`);
    loadSubs();
  }} catch {{ toast('インポートに失敗しました', false); }}
  event.target.value = '';
}}

/* ── Scheduler Run ────────────────────────────────────────────── */
async function schedulerRun() {{
  if (!confirm('配信タイミングに達した購読をすべて配信しますか？')) return;
  try {{
    const r = await fetch('/v1/scheduler/run', {{method:'POST',headers:{{'Content-Length':'0'}}}});
    const d = await r.json();
    toast(`${{d.processed}}件を処理しました（送信: ${{d.sent}}件）`);
    loadSubs();
    loadHistStats();
    const sr = $('sched-result');
    if (sr) sr.textContent = `処理完了: ${{d.processed}}件処理・${{d.sent}}件送信 (${{new Date().toLocaleTimeString('ja-JP')}})`;
  }} catch {{ toast('実行失敗', false); }}
}}

/* ── Preview ──────────────────────────────────────────────────── */
// Quick preview from form
$('btn-preview-quick').addEventListener('click', () => {{
  const t = $('f-theme').value.trim();
  if (!t) {{ toast('テーマを入力してください', false); return; }}
  $('pv-theme').value = t;
  document.querySelectorAll('.tab,.tab-panel').forEach(el => el.classList.remove('active'));
  document.querySelector('[data-tab="preview"]').classList.add('active');
  $('tab-preview').classList.add('active');
  doPreview();
}});

async function doPreview() {{
  const theme = $('pv-theme').value.trim();
  const count = Number($('pv-count').value)||5;
  if (!theme) {{ toast('テーマを入力してください', false); return; }}
  $('pv-lbl').innerHTML = '<span class="spin"></span>';
  $('btn-preview').disabled = true;
  $('prev-list').innerHTML = '<div class="pld"><span class="spin"></span> 記事を取得中...</div>';
  $('prev-srcs').innerHTML = '';
  try {{
    const r = await fetch('/v1/preview', {{
      method:'POST', headers:{{'Content-Type':'application/json'}},
      body: JSON.stringify({{theme, count}})
    }});
    const d = await r.json();
    if (!r.ok) {{ $('prev-list').innerHTML = `<div class="pld" style="color:var(--red)">❌ ${{d.detail||'取得失敗'}}</div>`; return; }}

    // Sources
    const srcs = d.sources||[];
    $('prev-srcs').innerHTML = srcs.length
      ? '<span class="slbl">📰 情報源:</span>' + srcs.map(s=>`<span class="sbadge">${{s}}</span>`).join('')
      : '';
    // Summarizer badge
    $('prov-lbl').innerHTML = `<span class="prov-badge">🤖 ${{d.summarizer||'rule-based'}}</span>`;

    // Articles
    const items = d.items||[];
    if (!items.length) {{
      $('prev-list').innerHTML = '<div id="prev-ph"><span style="font-size:2rem">📭</span>記事が見つかりませんでした</div>';
      return;
    }}
    $('prev-list').innerHTML = items.map(it => `
      <div class="ac">
        <div class="ac-meta">
          <span class="ac-media">${{it.media||'不明'}}</span>
          <span class="ac-date">${{fmtDateISO(it.published)}}</span>
        </div>
        <div class="ac-title">${{it.subject}}</div>
        <div class="ac-sum">${{it.summary}}</div>
        <a href="${{it.url}}" target="_blank" rel="noopener" class="ac-link">記事を読む →</a>
      </div>`).join('');
  }} catch {{ $('prev-list').innerHTML = '<div class="pld" style="color:var(--red)">❌ 通信エラー</div>'; }}
  finally {{ $('pv-lbl').textContent = '👁 プレビュー'; $('btn-preview').disabled = false; }}
}}

/* ── History ──────────────────────────────────────────────────── */
async function loadHistStats() {{
  try {{
    const r = await fetch('/v1/history/stats');
    const d = await r.json();
    $('st-hist').textContent  = d.total_deliveries;
    $('hs-total').textContent = d.total_deliveries;
    $('hs-arts').textContent  = d.total_articles;
    $('hs-themes').textContent= d.unique_themes;
    $('hs-last').textContent  = d.last_sent_at ? fmtDate(d.last_sent_at) : '—';
  }} catch {{}}
  /* 配信実績チャート用に直近履歴を取得して描画 */
  try {{
    const hr = await fetch('/v1/history?limit=500');
    const records = await hr.json();
    historyData = records;
    updateDeliveryChart(records);
  }} catch {{}}
}}

async function loadHistory() {{
  const theme  = $('hist-filter-theme').value.trim();
  const status = $('hist-filter-status').value;
  try {{
    const url = '/v1/history?limit=200' + (theme ? '&theme=' + encodeURIComponent(theme) : '');
    const r = await fetch(url);
    historyData = await r.json();
    renderHistory(historyData, status);
    loadHistStats();
  }} catch {{ toast('履歴取得に失敗', false); }}
}}

function applyHistFilter() {{
  renderHistory(historyData, $('hist-filter-status').value);
}}

function renderHistory(data, status) {{
  let rows = data;
  if (status === 'success') rows = rows.filter(r => r.success);
  if (status === 'fail')    rows = rows.filter(r => !r.success);
  if (!rows.length) {{
    $('hist-tbody').innerHTML = '<tr><td colspan="6" style="text-align:center;color:var(--muted);padding:1.5rem">履歴がありません</td></tr>';
    return;
  }}
  $('hist-tbody').innerHTML = rows.map(r => `
    <tr>
      <td style="white-space:nowrap">${{fmtDate(r.sent_at)}}</td>
      <td style="max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${{r.theme}}">${{r.theme}}</td>
      <td style="max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${{r.email}}">${{r.email}}</td>
      <td style="text-align:center">${{r.item_count}}</td>
      <td style="font-size:.72rem;color:var(--muted)">${{(r.sources||[]).slice(0,3).join(' / ')}}</td>
      <td class="${{r.success?'hist-success':'hist-fail'}}">${{r.success?'✅ 成功':'❌ 失敗'}}</td>
    </tr>`).join('');
}}

/* ── Charts ───────────────────────────────────────────────────── */
function updateDeliveryChart(records) {{
  /* 直近7日間（今日含む）の配信成功/失敗を積み上げ棒グラフで表示 */
  const now = Date.now();
  const days = [], successArr = [], failArr = [];
  for (let i = 6; i >= 0; i--) {{
    const d = new Date(now - i * 86400000);
    days.push((d.getMonth()+1) + '/' + d.getDate());
    successArr.push(0);
    failArr.push(0);
  }}
  records.forEach(r => {{
    const d = new Date(r.sent_at * 1000);
    const label = (d.getMonth()+1) + '/' + d.getDate();
    const idx = days.indexOf(label);
    if (idx < 0) return;
    if (r.success) successArr[idx]++; else failArr[idx]++;
  }});
  const cdata = {{
    labels: days,
    datasets: [
      {{label:'成功', data:successArr, backgroundColor:'rgba(16,185,129,.75)', borderRadius:4, borderSkipped:false}},
      {{label:'失敗', data:failArr,    backgroundColor:'rgba(239,68,68,.7)',   borderRadius:4, borderSkipped:false}}
    ]
  }};
  const opts = {{
    responsive:true,
    plugins:{{
      legend:{{position:'top',labels:{{color:'#94a3b8',font:{{size:11}},boxWidth:12}}}},
      tooltip:{{callbacks:{{label:c=>` ${{c.dataset.label}}: ${{c.raw}}件`}}}}
    }},
    scales:{{
      x:{{stacked:true,grid:{{color:'rgba(255,255,255,.04)'}},ticks:{{color:'#64748b'}}}},
      y:{{stacked:true,beginAtZero:true,grid:{{color:'rgba(255,255,255,.04)'}},
         ticks:{{color:'#64748b',stepSize:1,precision:0}}}}
    }}
  }};
  if (freqChart) {{ freqChart.data=cdata; freqChart.options=opts; freqChart.update(); return; }}
  Chart.defaults.color='#64748b';
  freqChart = new Chart($('freqChart').getContext('2d'),{{type:'bar',data:cdata,options:opts}});
}}

function updateIntensChart(subs) {{
  const score = {{}};
  const iv = {{hourly:3600,daily:86400,weekly:604800,monthly:2592000}};
  const WEEK = 604800;
  subs.filter(s=>s.active).forEach(s => {{
    const w = (WEEK / (iv[s.frequency]||86400)) * s.count;
    score[s.theme] = (score[s.theme]||0) + w;
  }});
  const entries = Object.entries(score).sort((a,b)=>b[1]-a[1]).slice(0,8);
  const labels = entries.map(e=>e[0]);
  const data   = entries.map(e=>Math.round(e[1]));
  const cdata = {{labels,datasets:[{{label:'週あたり本数',data,backgroundColor:'#6366f1',borderRadius:5}}]}};
  const opts  = {{
    indexAxis:'y',responsive:true,
    plugins:{{legend:{{display:false}},tooltip:{{callbacks:{{label:c=>` ${{c.raw}} 本/週`}}}}}},
    scales:{{
      x:{{beginAtZero:true,grid:{{color:'rgba(255,255,255,.04)'}},ticks:{{color:'#64748b'}}}},
      y:{{grid:{{display:false}},ticks:{{color:'#94a3b8',font:{{size:11}}}}}}
    }}
  }};
  if (intensChart) {{ intensChart.data=cdata; intensChart.update(); return; }}
  intensChart = new Chart($('intensityChart').getContext('2d'),{{type:'bar',data:cdata,options:opts}});
}}

/* ── Status Check ─────────────────────────────────────────────── */
async function checkStatus() {{
  try {{
    const r = await fetch('/health');
    if (!r.ok) return;
    const d = await r.json();
    const smtp = d.smtp_configured;
    $('h-smtp').textContent = smtp ? 'SMTP ✓' : 'SMTP 未設定';
    $('h-smtp').style.color = smtp ? 'var(--green)' : 'var(--yellow)';
    $('h-summ').textContent = `要約: ${{d.summarizer||'rule-based'}}`;
    $('h-summ').style.color = d.summarizer==='anthropic'||d.summarizer==='openai' ? 'var(--green)' : 'var(--muted)';
  }} catch {{
    $('h-status').textContent = 'オフライン';
    $('h-status').style.color = 'var(--red)';
  }}
}}

/* ── Init ─────────────────────────────────────────────────────── */
$('filter-freq').addEventListener('change', loadSubs);
// ログインユーザーのメールを配信先フィールドに自動セット
(function() {{
  const me = '{user_email}';
  const emailField = $('f-email');
  if (me && emailField && !emailField.value) {{
    emailField.value = me;
  }}
}})();
loadSubs();
loadHistStats();
checkStatus();
setInterval(loadSubs, 30000);
</script>
</body>
</html>
"""

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ログインページ HTML
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _build_login_html(sent: str = "", error: str = "") -> str:
    """ログインページ HTML を組み立てる。"""
    sent_msg = (
        '<div class="msg-ok">&#x2705; ログインリンクを送信しました。メールをご確認ください。</div>'
        if sent == "1" else ""
    )
    error_msg = (
        f'<div class="msg-err">&#x26A0; {error}</div>'
        if error else ""
    )
    return f"""\
<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>News Digest &#x2014; ログイン</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
:root{{--bg:#080d1a;--s1:#0d1526;--s2:#131f35;--border:rgba(255,255,255,.07);
  --text:#f1f5f9;--t2:#cbd5e1;--muted:#64748b;
  --brand:#6366f1;--blight:#818cf8;--r:14px;}}
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0;}}
body{{background:var(--bg);color:var(--text);font-family:'Inter','Segoe UI',system-ui,sans-serif;
  min-height:100vh;display:flex;align-items:center;justify-content:center;padding:1rem;}}
.card{{background:var(--s1);border:1px solid var(--border);border-radius:var(--r);
  padding:2.5rem;width:100%;max-width:420px;box-shadow:0 20px 60px rgba(0,0,0,.5);}}
.logo{{display:flex;align-items:center;gap:.6rem;font-size:1.1rem;font-weight:800;
  margin-bottom:2rem;justify-content:center;}}
.logo-icon{{width:36px;height:36px;border-radius:9px;
  background:linear-gradient(135deg,#6366f1,#06b6d4);
  display:flex;align-items:center;justify-content:center;font-size:1.1rem;}}
h2{{font-size:1.4rem;font-weight:700;margin-bottom:.4rem;text-align:center;}}
.sub{{color:var(--muted);font-size:.85rem;text-align:center;margin-bottom:1.8rem;}}
label{{display:block;font-size:.8rem;font-weight:500;color:var(--t2);margin-bottom:.4rem;}}
input[type=email]{{width:100%;background:var(--s2);border:1px solid var(--border);color:var(--text);
  border-radius:9px;padding:.75rem 1rem;font-size:.9rem;outline:none;transition:border-color .2s;}}
input[type=email]:focus{{border-color:var(--brand);box-shadow:0 0 0 3px rgba(99,102,241,.18);}}
.btn{{width:100%;margin-top:1rem;padding:.85rem;background:var(--brand);
  color:#fff;border:none;border-radius:9px;font-size:.95rem;font-weight:600;
  cursor:pointer;transition:background .2s;}}
.btn:hover{{background:var(--blight);}}
.msg-ok{{background:rgba(16,185,129,.12);border:1px solid rgba(16,185,129,.3);
  color:#6ee7b7;border-radius:9px;padding:.9rem 1rem;margin-bottom:1.2rem;
  font-size:.85rem;text-align:center;}}
.msg-err{{background:rgba(239,68,68,.12);border:1px solid rgba(239,68,68,.3);
  color:#fca5a5;border-radius:9px;padding:.9rem 1rem;margin-bottom:1.2rem;
  font-size:.85rem;text-align:center;}}
.note{{color:var(--muted);font-size:.78rem;text-align:center;margin-top:1.4rem;line-height:1.6;}}
</style>
</head>
<body>
<div class="card">
  <div class="logo"><div class="logo-icon">&#x1F4F0;</div>News Digest</div>
  <h2>ログイン</h2>
  <p class="sub">メールアドレスを入力するとログインリンクをお送りします。</p>
  {sent_msg}
  {error_msg}
  <form method="post" action="/login">
    <label for="email">メールアドレス</label>
    <input type="email" id="email" name="email" placeholder="you@example.com" required autofocus>
    <button class="btn" type="submit">ログインリンクを送信</button>
  </form>
  <p class="note">リンクは <strong>15 分間</strong> 有効です。<br>パスワードは不要です。</p>
</div>
</body>
</html>"""
