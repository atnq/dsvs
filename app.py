"""
ak47mail — interface de boîte jetable branchée sur l'API temp.tf (sans pub).

Lancer :  pip install -r requirements.txt && python app.py
Ouvrir :  http://127.0.0.1:5000
"""
import csv
import hmac
import io
import json
import os
import threading
import time
from pathlib import Path

import requests
from flask import Flask, Response, jsonify, request, send_file

API = "https://temp.tf/api"
HERE = Path(__file__).parent
FILE = HERE / "a.txt"
DELAY = 1.2  # secondes entre deux requêtes pour le comptage a.txt (limite : 60/min)

app = Flask(__name__)
lock = threading.Lock()
state = {"running": False, "done": 0, "total": 0, "results": {}}


# ---------- protection par mot de passe (optionnelle) ----------
# Sur l'hébergeur, définissez la variable d'environnement APP_PASSWORD
# pour que le site demande un mot de passe (identifiant : n'importe lequel).
PASSWORD = os.environ.get("APP_PASSWORD", "")


@app.before_request
def require_password():
    if not PASSWORD:
        return None
    auth = request.authorization
    if auth and hmac.compare_digest((auth.password or "").encode(), PASSWORD.encode()):
        return None
    return Response("Mot de passe requis", 401,
                    {"WWW-Authenticate": 'Basic realm="ak47mail"'})


# ---------- archivage dans Postgres (optionnel) ----------
# Définissez DATABASE_URL (Internal Database URL de Render) pour activer.
DATABASE_URL = os.environ.get("DATABASE_URL", "")
try:
    import psycopg2
except ImportError:  # sans le paquet, l'archivage reste désactivé
    psycopg2 = None
DB_ON = bool(DATABASE_URL and psycopg2)
_db_ready = False


def db():
    return psycopg2.connect(DATABASE_URL, connect_timeout=10)


def db_init():
    global _db_ready
    if _db_ready:
        return
    with db() as c, c.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                address TEXT NOT NULL,
                msg_id TEXT NOT NULL,
                subject TEXT,
                sender TEXT,
                date TEXT,
                body TEXT,
                body_type TEXT,
                attachments JSONB,
                saved_at TIMESTAMPTZ DEFAULT now(),
                PRIMARY KEY (address, msg_id)
            )""")
    _db_ready = True


def archive(email, msgs):
    """Enregistre les messages ; ne doit jamais faire échouer la requête."""
    if not DB_ON or not msgs:
        return
    try:
        db_init()
        with db() as c, c.cursor() as cur:
            for m in msgs:
                cur.execute(
                    """INSERT INTO messages
                       (address,msg_id,subject,sender,date,body,body_type,attachments)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (address,msg_id) DO NOTHING""",
                    (email, str(m.get("id")), m.get("subject"), m.get("from"),
                     m.get("date"), m.get("body"), m.get("bodyContentType"),
                     json.dumps(m.get("attachments") or [])))
    except Exception as e:  # noqa: BLE001
        app.logger.warning("archivage impossible : %s", e)


@app.get("/api/archive/stats")
def archive_stats():
    if not DB_ON:
        return jsonify(enabled=False)
    try:
        db_init()
        with db() as c, c.cursor() as cur:
            cur.execute("SELECT count(*), count(DISTINCT address) FROM messages")
            n, a = cur.fetchone()
        return jsonify(enabled=True, messages=n, addresses=a)
    except Exception as e:  # noqa: BLE001
        return jsonify(enabled=True, error=str(e)), 500


@app.get("/api/archive/export")
def archive_export():
    if not DB_ON:
        return jsonify(error="archivage désactivé (DATABASE_URL absente)"), 400
    fmt = request.args.get("format", "csv")
    addr = request.args.get("email")
    cols = ["address", "msg_id", "subject", "sender", "date", "body_type", "body", "saved_at"]
    q = f"SELECT {','.join(cols)} FROM messages" + (" WHERE address=%s" if addr else "") + " ORDER BY saved_at DESC"
    try:
        db_init()
        with db() as c, c.cursor() as cur:
            cur.execute(q, (addr,) if addr else None)
            rows = cur.fetchall()
    except Exception as e:  # noqa: BLE001
        return jsonify(error=str(e)), 500
    if fmt == "json":
        data = [dict(zip(cols, [str(v) if k == "saved_at" else v for k, v in zip(cols, r)])) for r in rows]
        return Response(json.dumps(data, ensure_ascii=False, indent=1), mimetype="application/json",
                        headers={"Content-Disposition": "attachment; filename=mails.json"})
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    w.writerows(rows)
    return Response("\ufeff" + buf.getvalue(), mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=mails.csv"})


# ---------- proxy vers temp.tf ----------
def relay(method, path, **kw):
    try:
        r = requests.request(method, f"{API}{path}", timeout=25, **kw)
    except requests.RequestException as e:
        return jsonify(error=f"temp.tf injoignable ({e.__class__.__name__})"), 502
    try:
        body = r.json()
    except ValueError:
        body = {"error": f"réponse invalide ({r.status_code})"}
    resp = jsonify(body)
    resp.status_code = r.status_code
    if "Retry-After" in r.headers:
        resp.headers["Retry-After"] = r.headers["Retry-After"]
    return resp


@app.get("/api/account")
def account():
    return relay("GET", "/account", params=request.args)


@app.get("/api/stats")
def stats():
    return relay("GET", "/stats", params=request.args)


@app.post("/api/check")
def check():
    payload = request.get_json(silent=True) or {}
    resp = relay("POST", "/check", json=payload)
    if resp.status_code == 200 and payload.get("email"):
        archive(payload["email"], (resp.get_json() or {}).get("data", []))
    return resp


@app.get("/api/attachment")
def attachment():
    try:
        r = requests.get(f"{API}/attachment", params=request.args, timeout=60)
    except requests.RequestException:
        return jsonify(error="temp.tf injoignable"), 502
    headers = {"X-Content-Type-Options": "nosniff"}
    if "Content-Disposition" in r.headers:
        headers["Content-Disposition"] = r.headers["Content-Disposition"]
    return Response(r.content, status=r.status_code,
                    content_type=r.headers.get("Content-Type", "application/octet-stream"),
                    headers=headers)


# ---------- comptage des adresses de a.txt ----------
def read_addresses():
    if not FILE.exists():
        return []
    seen, out = set(), []
    for line in FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and line.lower() not in seen:
            seen.add(line.lower())
            out.append(line)
    return out


def check_one(email):
    try:
        r = requests.post(f"{API}/check", json={"email": email}, timeout=20)
    except requests.RequestException as e:
        return {"error": f"réseau : {e.__class__.__name__}"}, 0
    if r.status_code == 429:
        return None, int(r.headers.get("Retry-After", 30))
    if r.status_code == 403:
        return {"error": "adresse non générée par temp.tf"}, 0
    if r.status_code != 200:
        return {"error": f"erreur {r.status_code}"}, 0
    msgs = r.json().get("data", [])
    archive(email, msgs)
    last = msgs[0] if msgs else None
    return {
        "count": len(msgs),
        "last_subject": last["subject"] if last else "",
        "last_from": last["from"] if last else "",
    }, 0


def scan():
    addresses = read_addresses()
    with lock:
        state.update(running=True, done=0, total=len(addresses), results={})
    for email in addresses:
        while True:
            res, wait = check_one(email)
            if res is not None:
                break
            time.sleep(wait)
        with lock:
            state["results"][email] = res
            state["done"] += 1
        time.sleep(DELAY)
    with lock:
        state["running"] = False


@app.post("/api/scan")
def start_scan():
    with lock:
        if state["running"]:
            return jsonify(ok=False, error="déjà en cours"), 409
    threading.Thread(target=scan, daemon=True).start()
    return jsonify(ok=True)


@app.get("/api/state")
def get_state():
    addresses = read_addresses()
    with lock:
        results = dict(state["results"])
        rows = [{"email": a, **results.get(a, {})} for a in addresses]
        return jsonify(
            running=state["running"], done=state["done"], total=state["total"],
            addresses=len(addresses),
            total_msgs=sum(r.get("count", 0) for r in results.values()),
            rows=rows,
        )


@app.get("/")
def index():
    return send_file(HERE / "index.html")


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5000)))
