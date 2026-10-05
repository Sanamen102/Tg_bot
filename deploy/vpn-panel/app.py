#!/usr/bin/env python3
"""Веб-панель управления VPN.

Зачем отдельным сервисом, а не страницей в боте: раздача подписок и так
живёт на домашнем сервере, и людям удобнее открыть ссылку, чем искать
команду в Telegram. Управление при этом ходит туда же, куда и бот, —
в forced-command обёртку vpn-bot-ctl на VPS, так что второго пути к
серверу не появляется и периметр не расширяется.

Раздача самих подписок (/sub/) остаётся статикой nginx и через это
приложение НЕ проходит: она обязана работать, даже когда VPS недоступен
или панель упала.
"""

import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import subprocess
import time
from functools import wraps

from flask import (Flask, abort, flash, redirect, render_template, request,
                   session, url_for)

VPS_HOST = os.environ.get("VPN_PANEL_HOST", "147.45.72.72")
VPS_USER = os.environ.get("VPN_PANEL_USER", "root")
SSH_KEY = os.environ.get("VPN_PANEL_KEY", "/app/ssh/id_ed25519_vpn")
LOGIN = os.environ.get("VPN_PANEL_LOGIN", "admin")
PASSWORD_HASH = os.environ.get("VPN_PANEL_PASSWORD_HASH", "")
SSH_TIMEOUT = int(os.environ.get("VPN_PANEL_SSH_TIMEOUT", "60"))

# Панель открыта в интернет, поэтому перебор пароля ограничиваем и на
# уровне приложения — не полагаясь только на nginx и fail2ban.
FAIL_LIMIT = 5
FAIL_WINDOW = 300
_fails = {}

app = Flask(__name__)
app.secret_key = os.environ.get("VPN_PANEL_SECRET", secrets.token_hex(32))
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("VPN_PANEL_INSECURE") != "1",
    PERMANENT_SESSION_LIFETIME=60 * 60 * 12,
)


def ctl(action, arg=None):
    """Вызов forced-command обёртки на VPS. Возвращает разобранный JSON.

    Ключ прибит к vpn-bot-ctl, поэтому даже утёкший контейнер не даёт
    shell на VPS — обёртка принимает только семь действий и валидирует
    аргументы сама.
    """
    cmd = ["ssh", "-i", SSH_KEY, "-n",
           "-o", "BatchMode=yes",
           "-o", "ConnectTimeout=15",
           "-o", "StrictHostKeyChecking=accept-new",
           "%s@%s" % (VPS_USER, VPS_HOST),
           action if arg is None else "%s %s" % (action, arg)]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=SSH_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "VPS не ответил вовремя"}
    if p.returncode != 0 and not p.stdout.strip():
        return {"ok": False,
                "error": (p.stderr or "ssh вернул %d" % p.returncode).strip()[:200]}
    try:
        return json.loads(p.stdout)
    except ValueError:
        return {"ok": False, "error": "непонятный ответ: %s" % p.stdout.strip()[:200]}


def human_bytes(n):
    n = float(n or 0)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if n < 1024 or unit == "ТБ":
            return "%.0f %s" % (n, unit) if unit == "Б" else "%.1f %s" % (n, unit)
        n /= 1024


def human_when(iso):
    """«2 часа назад» вместо ISO-строки: так сразу видно, кто отвалился."""
    if not iso:
        return "никогда"
    try:
        from datetime import datetime, timezone
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        d = (datetime.now(timezone.utc) - t).total_seconds()
    except Exception:
        return iso
    if d < 120:
        return "только что"
    if d < 3600:
        return "%d мин назад" % (d // 60)
    if d < 86400:
        return "%d ч назад" % (d // 3600)
    return "%d дн назад" % (d // 86400)


app.jinja_env.filters["bytes"] = human_bytes
app.jinja_env.filters["when"] = human_when


def blocked(ip):
    tries = [t for t in _fails.get(ip, []) if time.time() - t < FAIL_WINDOW]
    _fails[ip] = tries
    return len(tries) >= FAIL_LIMIT


def note_fail(ip):
    _fails.setdefault(ip, []).append(time.time())


def _from_proxy(addr):
    # nginx живёт на хосте и ходит на 127.0.0.1:8091 — до контейнера такой
    # запрос доходит со шлюза docker-сети (172.16.0.0/12). Из локальной сети
    # на 8091 приходят напрямую, со своего 192.168.x.x.
    try:
        ip = ipaddress.ip_address(addr or "")
    except ValueError:
        return False
    return ip.is_loopback or ip in ipaddress.ip_network("172.16.0.0/12")


def client_ip():
    # Заголовкам верим только от nginx: он перезаписывает X-Real-IP своим
    # $remote_addr. Иначе любой в локальной сети подставлял бы свой
    # X-Real-IP и обходил лимит попыток входа.
    if _from_proxy(request.remote_addr):
        fwd = request.headers.get("X-Real-IP") or request.headers.get("X-Forwarded-For", "")
        ip = fwd.split(",")[0].strip()
        if ip:
            return ip
    return request.remote_addr or "?"


def _safe_next(target):
    # Только пути внутри панели: иначе ссылка вида /login?next=https://чужой
    # после верного пароля уводила бы на сторонний сайт
    if target and target.startswith("/") and not target.startswith("//") \
            and "\\" not in target:
        return target
    return url_for("index")


def login_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not session.get("auth"):
            return redirect(url_for("login", next=request.path))
        return f(*a, **kw)
    return wrapper


def check_csrf():
    """POST без совпадающего токена не выполняем: панель под паролем, но
    ссылку на неё могут скормить браузеру со стороны."""
    # Сравниваем байты: compare_digest на str с не-ASCII символами бросает
    # TypeError, и вместо 400 получался 500
    if not hmac.compare_digest(request.form.get("csrf", "").encode(),
                               session.get("csrf", "").encode()):
        abort(400)


@app.before_request
def ensure_csrf():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        ip = client_ip()
        if blocked(ip):
            flash("Слишком много попыток. Подождите пять минут.", "err")
            return render_template("login.html"), 429
        # Логин сверяем тем же compare_digest, что и пароль: сравнение по
        # времени не должно подсказывать, угадано ли имя.
        name_ok = hmac.compare_digest(request.form.get("login", "").encode(), LOGIN.encode())
        given = hashlib.sha256(request.form.get("password", "").encode()).hexdigest()
        if PASSWORD_HASH and name_ok and hmac.compare_digest(given, PASSWORD_HASH):
            session.clear()
            session["auth"] = True
            session["csrf"] = secrets.token_urlsafe(32)
            session.permanent = True
            app.logger.info("вход с %s", ip)
            return redirect(_safe_next(request.args.get("next")))
        note_fail(ip)
        app.logger.warning("неудачный вход с %s", ip)
        flash("Неверный логин или пароль", "err")
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    check_csrf()
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    users = ctl("users")
    status = ctl("status")
    return render_template("index.html", users=users, status=status,
                           csrf=session["csrf"])


@app.route("/add", methods=["POST"])
@login_required
def add():
    check_csrf()
    name = (request.form.get("name") or "").strip()
    r = ctl("add", name)
    if r.get("ok"):
        flash("Готов «%s». Ссылка на подписку: %s" % (name, r.get("sub")), "ok")
    else:
        flash("Не вышло: %s" % r.get("error"), "err")
    return redirect(url_for("index"))


@app.route("/del", methods=["POST"])
@login_required
def delete():
    check_csrf()
    name = (request.form.get("name") or "").strip()
    r = ctl("del", name)
    if r.get("ok"):
        flash("Доступ «%s» отозван, осталось %s" % (name, r.get("left")), "ok")
    else:
        flash("Не вышло: %s" % r.get("error"), "err")
    return redirect(url_for("index"))


@app.route("/user/<name>")
@login_required
def user(name):
    sub = ctl("sub", name)
    link = ctl("link", name)
    if not sub.get("ok"):
        flash("Не вышло: %s" % sub.get("error"), "err")
        return redirect(url_for("index"))
    return render_template("user.html", name=name, sub=sub.get("value"),
                           link=link.get("value"), csrf=session["csrf"])


@app.route("/qr")
@login_required
def qr():
    """QR рисуем на сервере: клиенту не нужен интернет и сторонние скрипты."""
    import io
    import segno
    data = request.args.get("d", "")
    if not data:
        abort(400)
    buf = io.BytesIO()
    segno.make(data, error="m").save(buf, kind="svg", scale=5, dark="#111")
    return buf.getvalue(), 200, {"Content-Type": "image/svg+xml"}


@app.route("/server", methods=["POST"])
@login_required
def server():
    check_csrf()
    addr = (request.form.get("addr") or "").strip()
    r = ctl("server", addr)
    if r.get("ok"):
        flash("Адрес VPN: %s → %s, переписано подписок: %s. "
              "Ссылки у людей не изменились." % (r.get("old"), r.get("new"),
                                                 r.get("regenerated")), "ok")
    else:
        flash("Не вышло: %s" % r.get("error"), "err")
    return redirect(url_for("index"))


@app.route("/healthz")
def healthz():
    return "ok", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8091)
