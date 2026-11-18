#!/usr/bin/env python3
"""
NP6 Register Simulator – full backend (server.py)

Serves the static frontend and the /api/* JSON API used by js/db.js.

Usage:
    python server.py
    # then open http://127.0.0.1:8686/

Default owner crew ID: 1039
Admin UI: http://127.0.0.1:8686/admin.html

Requires Python 3.8+ (stdlib only).
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
import socket
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
HOST = os.environ.get("NP6_HOST", "0.0.0.0")
PORT = int(os.environ.get("NP6_PORT", "8687"))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("NP6_DB", os.path.join(BASE_DIR, "np6sim.db"))
STATIC_ROOT = BASE_DIR
REGISTER_TTL = 90  # seconds without heartbeat before a register claim expires

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
_lock = threading.RLock()


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    with _lock:
        conn = db()
        c = conn.cursor()
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id          TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                role        TEXT NOT NULL CHECK(role IN ('crew','manager','owner')),
                login_count INTEGER DEFAULT 0,
                last_login  INTEGER,
                created_at  INTEGER
            );

            CREATE TABLE IF NOT EXISTS sessions (
                token       TEXT PRIMARY KEY,
                user_id     TEXT NOT NULL,
                pos         INTEGER,
                pod         TEXT,
                created_at  INTEGER,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS registers (
                pos         INTEGER PRIMARY KEY,
                user_id     TEXT,
                name        TEXT,
                pod         TEXT,
                last_seen   INTEGER
            );

            CREATE TABLE IF NOT EXISTS orders (
                uid         TEXT PRIMARY KEY,
                user_id     TEXT,
                pos         INTEGER,
                pod         TEXT,
                status      TEXT,
                total       INTEGER,
                data        TEXT,
                created_at  INTEGER
            );

            CREATE TABLE IF NOT EXISTS loyalty (
                id          TEXT PRIMARY KEY,
                code        TEXT UNIQUE NOT NULL,
                name        TEXT NOT NULL,
                points      INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS counters (
                name        TEXT PRIMARY KEY,
                value       INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS collections (
                col         TEXT NOT NULL,
                id          TEXT NOT NULL,
                doc         TEXT NOT NULL,
                PRIMARY KEY (col, id)
            );
            """
        )
        now = int(time.time() * 1000)
        if c.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
            c.execute(
                "INSERT INTO users (id, name, role, login_count, created_at) VALUES (?,?,?,?,?)",
                ("1039", "Owner", "owner", 0, now),
            )
            for uid, name, role in [
                ("1001", "Alex", "crew"),
                ("1002", "Sam", "crew"),
                ("1003", "Jordan", "manager"),
                ("1004", "Taylor", "crew"),
                ("1005", "Riley", "crew"),
            ]:
                c.execute(
                    "INSERT INTO users (id, name, role, login_count, created_at) VALUES (?,?,?,?,?)",
                    (uid, name, role, 0, now),
                )
        if c.execute("SELECT COUNT(*) FROM loyalty").fetchone()[0] == 0:
            for code, name, pts in [
                ("1001", "Alex", 7400),
                ("1002", "Sam", 2600),
                ("1003", "Jordan", 12850),
                ("1004", "Taylor", 900),
                ("1005", "Riley", 4700),
            ]:
                c.execute(
                    "INSERT INTO loyalty (id, code, name, points) VALUES (?,?,?,?)",
                    (f"m{code}", code, name, pts),
                )
        conn.commit()
        conn.close()


def user_with_stats(conn: sqlite3.Connection, u: sqlite3.Row) -> Dict[str, Any]:
    paid = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(total),0) FROM orders WHERE user_id=? AND status='paid'",
        (u["id"],),
    ).fetchone()
    return {
        "id": u["id"],
        "name": u["name"],
        "role": u["role"],
        "login_count": u["login_count"] or 0,
        "last_login": u["last_login"],
        "created_at": u["created_at"],
        "orders_count": paid[0],
        "sales_cents": paid[1],
    }


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
def json_body(handler) -> Any:
    length = int(handler.headers.get("Content-Length") or 0)
    if length <= 0:
        return None
    raw = handler.rfile.read(length)
    if not raw:
        return None
    return json.loads(raw.decode("utf-8"))


def send_json(handler, obj: Any, status: int = 200) -> None:
    data = json.dumps(obj, separators=(",", ":"), default=str).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type, X-NP6-Token")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(data)


def error(handler, msg: str, status: int = 400) -> None:
    send_json(handler, {"error": msg}, status)


def get_token(handler) -> Optional[str]:
    return handler.headers.get("X-NP6-Token") or None


def require_user(handler, conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    token = get_token(handler)
    if not token:
        return None
    return conn.execute(
        "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token=?",
        (token,),
    ).fetchone()


def lan_urls() -> List[str]:
    urls: List[str] = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith("127."):
            urls.append(f"http://{ip}:{PORT}/")
    except Exception:
        pass
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127."):
                u = f"http://{ip}:{PORT}/"
                if u not in urls:
                    urls.append(u)
    except Exception:
        pass
    if not urls:
        urls.append(f"http://127.0.0.1:{PORT}/")
    return urls


def owner_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM users WHERE role='owner'").fetchone()[0]


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
def handle_api(handler, method: str, path: str, query: Dict[str, List[str]]) -> None:
    parts = [unquote(p) for p in path.strip("/").split("/") if p]
    if len(parts) < 2 or parts[0] != "api":
        return error(handler, "Not found", 404)

    resource = parts[1]
    rest = parts[2:]

    with _lock:
        conn = db()
        try:
            # health
            if resource == "health" and method == "GET":
                return send_json(
                    handler,
                    {"ok": True, "engine": "OpenNP6", "lan": lan_urls()},
                )

            # registers
            if resource == "registers" and method == "GET":
                cutoff = int(time.time() * 1000) - REGISTER_TTL * 1000
                rows = conn.execute(
                    "SELECT pos, name, pod, last_seen FROM registers WHERE last_seen > ? ORDER BY pos",
                    (cutoff,),
                ).fetchall()
                return send_json(
                    handler,
                    [
                        {
                            "pos": r["pos"],
                            "name": r["name"],
                            "pod": r["pod"],
                            "at": r["last_seen"],
                        }
                        for r in rows
                    ],
                )

            # login
            if resource == "login" and method == "POST":
                body = json_body(handler) or {}
                uid = str(body.get("id") or "").strip()
                pos = int(body.get("pos") or 1)
                pod = str(body.get("pod") or "Front Counter")
                if not uid:
                    return error(handler, "Missing crew ID")
                u = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
                if not u:
                    return error(handler, "Unknown crew ID", 401)
                lock = conn.execute(
                    "SELECT doc FROM collections WHERE col='store' AND id='lockdown'"
                ).fetchone()
                if lock:
                    try:
                        ld = json.loads(lock["doc"])
                        if ld.get("locked") and u["role"] != "owner":
                            return error(
                                handler,
                                "The POS system is locked by the owner. Owner crew ID required.",
                                403,
                            )
                    except Exception:
                        pass
                now = int(time.time() * 1000)
                conn.execute(
                    "UPDATE users SET login_count = login_count + 1, last_login = ? WHERE id=?",
                    (now, uid),
                )
                token = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO sessions (token, user_id, pos, pod, created_at) VALUES (?,?,?,?,?)",
                    (token, uid, pos, pod, now),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO registers (pos, user_id, name, pod, last_seen) VALUES (?,?,?,?,?)",
                    (pos, uid, u["name"], pod, now),
                )
                conn.commit()
                out = user_with_stats(conn, u)
                out["token"] = token
                return send_json(handler, out)

            # heartbeat
            if resource == "heartbeat" and method == "POST":
                body = json_body(handler) or {}
                pos = int(body.get("pos") or 1)
                pod = str(body.get("pod") or "Front Counter")
                u = require_user(handler, conn)
                if not u:
                    return error(handler, "Unauthorized", 401)
                now = int(time.time() * 1000)
                conn.execute(
                    "INSERT OR REPLACE INTO registers (pos, user_id, name, pod, last_seen) VALUES (?,?,?,?,?)",
                    (pos, u["id"], u["name"], pod, now),
                )
                conn.commit()
                return send_json(handler, {"ok": True})

            # auth
            if resource == "auth":
                if rest == ["logout"] and method == "POST":
                    token = get_token(handler)
                    if token:
                        sess = conn.execute(
                            "SELECT pos FROM sessions WHERE token=?", (token,)
                        ).fetchone()
                        conn.execute("DELETE FROM sessions WHERE token=?", (token,))
                        if sess and sess["pos"] is not None:
                            conn.execute(
                                "DELETE FROM registers WHERE pos=?", (sess["pos"],)
                            )
                        conn.commit()
                    return send_json(handler, {"ok": True})
                if rest == ["manager"] and method == "POST":
                    body = json_body(handler) or {}
                    uid = str(body.get("id") or "").strip()
                    u = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
                    if not u or u["role"] == "crew":
                        return error(handler, "Not a manager ID", 403)
                    return send_json(handler, user_with_stats(conn, u))
                if rest == ["owner"] and method == "POST":
                    body = json_body(handler) or {}
                    uid = str(body.get("id") or "").strip()
                    u = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
                    if not u or u["role"] != "owner":
                        return error(handler, "Not an owner ID", 403)
                    return send_json(handler, user_with_stats(conn, u))
                if rest == ["manager", "logout"] and method == "POST":
                    return send_json(handler, {"ok": True})

            # users
            if resource == "users":
                if method == "GET" and not rest:
                    rows = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
                    return send_json(
                        handler, [user_with_stats(conn, r) for r in rows]
                    )
                if len(rest) == 1:
                    uid = rest[0]
                    if method == "PUT":
                        body = json_body(handler) or {}
                        name = str(body.get("name") or "").strip() or "Crew"
                        role = body.get("role") or "crew"
                        if role not in ("crew", "manager", "owner"):
                            role = "crew"
                        existing = conn.execute(
                            "SELECT * FROM users WHERE id=?", (uid,)
                        ).fetchone()
                        if (
                            existing
                            and existing["role"] == "owner"
                            and role != "owner"
                            and owner_count(conn) <= 1
                        ):
                            return error(
                                handler, "The store needs at least one owner."
                            )
                        now = int(time.time() * 1000)
                        if existing:
                            conn.execute(
                                "UPDATE users SET name=?, role=? WHERE id=?",
                                (name, role, uid),
                            )
                        else:
                            conn.execute(
                                "INSERT INTO users (id, name, role, login_count, created_at) VALUES (?,?,?,?,?)",
                                (uid, name, role, 0, now),
                            )
                        conn.commit()
                        u = conn.execute(
                            "SELECT * FROM users WHERE id=?", (uid,)
                        ).fetchone()
                        return send_json(handler, user_with_stats(conn, u))
                    if method == "DELETE":
                        existing = conn.execute(
                            "SELECT * FROM users WHERE id=?", (uid,)
                        ).fetchone()
                        if (
                            existing
                            and existing["role"] == "owner"
                            and owner_count(conn) <= 1
                        ):
                            return error(
                                handler, "The store needs at least one owner."
                            )
                        conn.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
                        conn.execute("DELETE FROM users WHERE id=?", (uid,))
                        conn.commit()
                        return send_json(handler, {"ok": True})

            # orders
            if resource == "orders":
                if method == "POST" and not rest:
                    body = json_body(handler) or {}
                    uid = body.get("uid") or uuid.uuid4().hex
                    now = int(time.time() * 1000)
                    conn.execute(
                        """INSERT OR REPLACE INTO orders
                           (uid, user_id, pos, pod, status, total, data, created_at)
                           VALUES (?,?,?,?,?,?,?,?)""",
                        (
                            uid,
                            body.get("user_id"),
                            body.get("pos"),
                            body.get("pod"),
                            body.get("status") or "paid",
                            int(body.get("total") or 0),
                            json.dumps(body),
                            int(body.get("at") or now),
                        ),
                    )
                    conn.commit()
                    return send_json(handler, {"ok": True})
                if method == "GET" and not rest:
                    since = int((query.get("since") or ["0"])[0] or 0)
                    limit = int((query.get("limit") or ["5000"])[0] or 5000)
                    limit = min(max(limit, 1), 10000)
                    rows = conn.execute(
                        "SELECT * FROM orders WHERE created_at >= ? ORDER BY created_at DESC LIMIT ?",
                        (since, limit),
                    ).fetchall()
                    out = []
                    for r in rows:
                        try:
                            data = json.loads(r["data"] or "{}")
                        except Exception:
                            data = {}
                        data["created_at"] = r["created_at"]
                        data["uid"] = r["uid"]
                        data["user_id"] = data.get("user_id") or r["user_id"]
                        data["pos"] = data.get("pos") if data.get("pos") is not None else r["pos"]
                        data["pod"] = data.get("pod") or r["pod"]
                        data["status"] = data.get("status") or r["status"]
                        data["total"] = data.get("total") if data.get("total") is not None else r["total"]
                        out.append(data)
                    return send_json(handler, out)

            # counters
            if resource == "counter" and len(rest) == 1 and method == "POST":
                name = rest[0]
                top = {"order": 999, "car": 99}.get(name, 999)
                row = conn.execute(
                    "SELECT value FROM counters WHERE name=?", (name,)
                ).fetchone()
                val = ((row["value"] if row else 0) % top) + 1
                conn.execute(
                    "INSERT OR REPLACE INTO counters (name, value) VALUES (?,?)",
                    (name, val),
                )
                conn.commit()
                return send_json(handler, {"value": val})

            # end of day
            if resource == "endofday" and method == "POST":
                now = int(time.time() * 1000)
                conn.execute("DELETE FROM counters")
                for col in ("kvs", "dtq", "cod"):
                    conn.execute("DELETE FROM collections WHERE col=?", (col,))
                conn.execute(
                    "INSERT OR REPLACE INTO collections (col, id, doc) VALUES (?,?,?)",
                    ("store", "day", json.dumps({"since": now})),
                )
                conn.commit()
                return send_json(handler, {"since": now})

            # join and kiosk endpoints (replacing legacy /api/loyalty)
            if resource == "join" and method == "POST":
                body = json_body(handler) or {}
                name = str(body.get("name") or "Guest").strip() or "Guest"
                while True:
                    code = str(1000 + (uuid.uuid4().int % 9000))
                    if not conn.execute(
                        "SELECT 1 FROM loyalty WHERE code=?", (code,)
                    ).fetchone():
                        break
                mid = f"m{code}{int(time.time()) % 10000}"
                conn.execute(
                    "INSERT INTO loyalty (id, code, name, points) VALUES (?,?,?,0)",
                    (mid, code, name),
                )
                conn.commit()
                return send_json(
                    handler,
                    {"id": mid, "code": code, "name": name, "points": 0},
                )

            if resource == "kiosk" and len(rest) >= 1:
                if rest[0] == "member" and method == "POST":
                    body = json_body(handler) or {}
                    code = str(body.get("code") or "").strip()
                    if not code:
                        return error(handler, "Missing code", 400)
                    row = conn.execute(
                        "SELECT * FROM loyalty WHERE code=?", (code,)
                    ).fetchone()
                    if not row:
                        return error(handler, "Code not found", 404)
                    return send_json(handler, {
                        "id": row["id"],
                        "code": row["code"],
                        "name": row["name"],
                        "points": row["points"] or 0,
                    })
                if rest[0] == "order" and method == "POST":
                    # A kiosk order waits in the 'stored' collection until a register brings it up
                    # (Get Mobile Order). It gets the next order number and a 4-digit pickup code.
                    body = json_body(handler) or {}
                    items = body.get("items") or []
                    if not items:
                        return error(handler, "Your order is empty.", 400)
                    now = int(time.time() * 1000)
                    row = conn.execute("SELECT value FROM counters WHERE name='order'").fetchone()
                    number = ((row["value"] if row else 0) % 999) + 1
                    conn.execute(
                        "INSERT OR REPLACE INTO counters (name, value) VALUES ('order', ?)", (number,)
                    )
                    taken = set()
                    for r in conn.execute("SELECT doc FROM collections WHERE col='stored'").fetchall():
                        try:
                            taken.add(str(json.loads(r["doc"]).get("kiosk", {}).get("code")))
                        except Exception:
                            pass
                    while True:
                        code = str(1000 + (uuid.uuid4().int % 9000))
                        if code not in taken:
                            break
                    uid = uuid.uuid4().hex
                    doc = {
                        "uid": uid,
                        "number": number,
                        "at": now,
                        "kiosk": {
                            "code": code,
                            "dine": body.get("dine"),
                            "table": body.get("table"),
                            "name": None,
                            "items": items,
                            "promo": body.get("promo"),
                            "mymcd": body.get("mymcd"),
                        },
                    }
                    conn.execute(
                        "INSERT OR REPLACE INTO collections (col, id, doc) VALUES ('stored', ?, ?)",
                        (uid, json.dumps(doc)),
                    )
                    conn.commit()
                    return send_json(handler, {"code": code, "number": number, "uid": uid})

            # legacy loyalty endpoints (kept for backwards compat)
            if resource == "loyalty":
                if method == "GET" and not rest:
                    rows = conn.execute(
                        "SELECT * FROM loyalty ORDER BY points DESC"
                    ).fetchall()
                    return send_json(handler, [dict(r) for r in rows])
                if method == "POST" and not rest:
                    body = json_body(handler) or {}
                    name = str(body.get("name") or "Guest").strip() or "Guest"
                    while True:
                        code = str(1000 + (uuid.uuid4().int % 9000))
                        if not conn.execute(
                            "SELECT 1 FROM loyalty WHERE code=?", (code,)
                        ).fetchone():
                            break
                    mid = f"m{code}{int(time.time()) % 10000}"
                    conn.execute(
                        "INSERT INTO loyalty (id, code, name, points) VALUES (?,?,?,0)",
                        (mid, code, name),
                    )
                    conn.commit()
                    return send_json(
                        handler,
                        {"id": mid, "code": code, "name": name, "points": 0},
                    )
                if len(rest) == 2 and rest[0] == "code" and method == "GET":
                    row = conn.execute(
                        "SELECT * FROM loyalty WHERE code=?", (rest[1],)
                    ).fetchone()
                    if not row:
                        return error(handler, "Code not found", 404)
                    return send_json(handler, dict(row))
                if len(rest) == 2 and rest[1] == "points" and method == "POST":
                    mid = rest[0]
                    body = json_body(handler) or {}
                    delta = int(body.get("delta") or 0)
                    row = conn.execute(
                        "SELECT * FROM loyalty WHERE id=?", (mid,)
                    ).fetchone()
                    if not row:
                        return error(handler, "Member not found", 404)
                    pts = max(0, (row["points"] or 0) + delta)
                    conn.execute(
                        "UPDATE loyalty SET points=? WHERE id=?", (pts, mid)
                    )
                    conn.commit()
                    return send_json(handler, {**dict(row), "points": pts})
                if len(rest) == 1:
                    mid = rest[0]
                    if method == "PUT":
                        body = json_body(handler) or {}
                        name = str(body.get("name") or "").strip()
                        conn.execute(
                            "UPDATE loyalty SET name=? WHERE id=?", (name, mid)
                        )
                        conn.commit()
                        row = conn.execute(
                            "SELECT * FROM loyalty WHERE id=?", (mid,)
                        ).fetchone()
                        return send_json(
                            handler, dict(row) if row else {"ok": True}
                        )
                    if method == "DELETE":
                        conn.execute("DELETE FROM loyalty WHERE id=?", (mid,))
                        conn.commit()
                        return send_json(handler, {"ok": True})

            # guest survey: a receipt code can be redeemed once, within 30 days of the sale
            if resource == "survey" and method == "POST" and rest == ["redeem"]:
                body = json_body(handler) or {}
                code = "".join(ch for ch in str(body.get("code") or "") if ch.isdigit())
                if len(code) != 26:
                    return error(handler, "Enter the 26-digit survey code from your receipt.", 400)
                row = conn.execute(
                    "SELECT doc FROM collections WHERE col='survey' AND id=?", (code,)
                ).fetchone()
                if not row:
                    return error(handler, "That survey code was not found. Check your receipt and try again.", 404)
                doc = json.loads(row["doc"])
                now = int(time.time() * 1000)
                if doc.get("used"):
                    return error(handler, "This survey code has already been used.", 409)
                if now - int(doc.get("at") or 0) > 30 * 86400 * 1000:
                    return error(handler, "This survey code has expired.", 410)
                answers = body.get("answers") or {}
                doc["used"] = True
                doc["usedAt"] = now
                conn.execute(
                    "UPDATE collections SET doc=? WHERE col='survey' AND id=?",
                    (json.dumps(doc), code),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO collections (col, id, doc) VALUES ('feedback', ?, ?)",
                    (code, json.dumps({**answers, "submitted_at": now, "order": doc.get("number"), "pos": doc.get("pos")})),
                )
                # 10% of surveys earn a 10%-off promo code (6 digits, single use, 30 days)
                promo = None
                if uuid.uuid4().int % 100 < 100:
                    while True:
                        pc = str(100000 + uuid.uuid4().int % 900000)
                        if not conn.execute("SELECT 1 FROM collections WHERE col='promo' AND id=?", (pc,)).fetchone():
                            break
                    conn.execute(
                        "INSERT INTO collections (col, id, doc) VALUES ('promo', ?, ?)",
                        (pc, json.dumps({"code": pc, "pct": 10, "label": "10% off your order", "used": False, "at": now, "source": "survey"})),
                    )
                    promo = {"code": pc, "pct": 10, "label": "10% off your order"}
                conn.commit()
                return send_json(handler, {"ok": True, "reward": doc.get("offer"), "promo": promo})

            # promo codes from survey receipts: check (kiosk) and use (register claims it, once)
            if resource == "promo" and method == "POST" and rest in (["check"], ["use"]):
                body = json_body(handler) or {}
                code = "".join(ch for ch in str(body.get("code") or "") if ch.isdigit())
                row = conn.execute(
                    "SELECT doc FROM collections WHERE col='promo' AND id=?", (code,)
                ).fetchone() if code else None
                if not row:
                    return error(handler, "That promo code wasn’t found.", 404)
                doc = json.loads(row["doc"])
                now = int(time.time() * 1000)
                if doc.get("used"):
                    return error(handler, "That promo code has already been used.", 409)
                if now - int(doc.get("at") or 0) > 30 * 86400 * 1000:
                    return error(handler, "That promo code has expired.", 410)
                if rest == ["use"]:
                    doc["used"] = True
                    doc["usedAt"] = now
                    conn.execute(
                        "UPDATE collections SET doc=? WHERE col='promo' AND id=?",
                        (json.dumps(doc), code),
                    )
                    conn.commit()
                return send_json(handler, {"ok": True, "code": code, "pct": doc.get("pct", 10), "label": doc.get("label")})

            # collections
            if resource == "col" and rest:
                col = rest[0]
                if len(rest) == 1:
                    if method == "GET":
                        rows = conn.execute(
                            "SELECT id, doc FROM collections WHERE col=?", (col,)
                        ).fetchall()
                        out: Dict[str, Any] = {}
                        for r in rows:
                            try:
                                out[r["id"]] = json.loads(r["doc"])
                            except Exception:
                                out[r["id"]] = r["doc"]
                        return send_json(handler, out)
                    if method == "DELETE":
                        conn.execute(
                            "DELETE FROM collections WHERE col=?", (col,)
                        )
                        conn.commit()
                        return send_json(handler, {"ok": True})
                if len(rest) == 2:
                    doc_id = rest[1]
                    if method == "PUT":
                        body = json_body(handler)
                        conn.execute(
                            "INSERT OR REPLACE INTO collections (col, id, doc) VALUES (?,?,?)",
                            (col, doc_id, json.dumps(body)),
                        )
                        conn.commit()
                        return send_json(handler, {"ok": True})
                    if method == "DELETE":
                        conn.execute(
                            "DELETE FROM collections WHERE col=? AND id=?",
                            (col, doc_id),
                        )
                        conn.commit()
                        return send_json(handler, {"ok": True})

            return error(handler, "Not found", 404)
        except json.JSONDecodeError:
            return error(handler, "Invalid JSON", 400)
        except Exception as e:
            return error(handler, str(e), 500)
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------
class NP6Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=STATIC_ROOT, **kwargs)

    def log_message(self, fmt, *args):
        try:
            print(f"[{self.log_date_time_string()}] {args[0]}")
        except Exception:
            pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-NP6-Token")
        self.send_header(
            "Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS"
        )
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            return handle_api(self, "GET", parsed.path, parse_qs(parsed.query))
        return super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            return handle_api(self, "POST", parsed.path, parse_qs(parsed.query))
        return error(self, "Method not allowed", 405)

    def do_PUT(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            return handle_api(self, "PUT", parsed.path, parse_qs(parsed.query))
        return error(self, "Method not allowed", 405)

    def do_DELETE(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            return handle_api(self, "DELETE", parsed.path, parse_qs(parsed.query))
        return error(self, "Method not allowed", 405)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    init_db()
    server = ThreadingHTTPServer((HOST, PORT), NP6Handler)
    print(f"NP6 simulator backend  http://{HOST}:{PORT}/")
    print(f"  Admin UI             http://127.0.0.1:{PORT}/admin.html")
    print(f"  Database             {DB_PATH}")
    print(f"  Default owner ID     1039")
    for u in lan_urls():
        print(f"  LAN                  {u}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down…")
        server.shutdown()


if __name__ == "__main__":
    main()
