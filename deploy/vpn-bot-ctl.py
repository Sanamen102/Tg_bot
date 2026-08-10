#!/usr/bin/env python3
"""Forced-command обёртка для ключа Telegram-бота.

Ключ бота в authorized_keys прибит к этому скрипту, поэтому что бы бот ни
прислал, выполнится только разрешённое действие из ACTIONS. Даже утёкший
ключ не даёт shell на VPS.

Аргументы приходят в SSH_ORIGINAL_COMMAND. Ответ всегда JSON.
"""
import json, os, re, shlex, subprocess, sys

CONF = "/root/mieru-server.json"
META = "/root/mieru-meta.json"
NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,32}$")
HOST_RE = re.compile(r"^[a-zA-Z0-9.-]{3,64}$")
ACTIONS = {"status", "users", "add", "del", "link", "sub", "server"}


def out(**kw):
    print(json.dumps(kw, ensure_ascii=False))
    sys.exit(0)


def fail(msg):
    out(ok=False, error=msg)


def sh(cmd, timeout=30):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def load(p):
    with open(p) as f:
        return json.load(f)


def mita_users():
    """LastActive и трафик по пользователям из таблицы mita get users."""
    rc, out_ = sh(["mita", "get", "users"])
    res = {}
    for line in out_.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and NAME_RE.match(parts[0]):
            res[parts[0]] = parts[1] if parts[1] != "-" else None
    return res


def traffic():
    rc, out_ = sh(["mita", "get", "metrics"])
    try:
        return json.loads(out_).get("users", {})
    except Exception:
        return {}


def sockets(proto):
    rc, out_ = sh(["bash", "-c",
                   "ss -%sln | awk '{print $4}' | grep -cE ':(3[0-9]{4})$'" % proto])
    return int(out_.strip() or 0)


def connections():
    rc, out_ = sh(["bash", "-c",
                   "mita get connections 2>/dev/null | awk 'NR>1 && $5==\"ESTABLISHED\"' | wc -l"])
    return int(out_.strip() or 0)


def portspec(cfg):
    for pb in cfg.get("portBindings", []):
        if pb.get("protocol") == "TCP":
            return pb.get("portRange") or str(pb.get("port"))
    return "?"


def links_map(kind):
    rc, out_ = sh(["mieru-user", kind])
    res = {}
    for line in out_.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and NAME_RE.match(parts[0]):
            res[parts[0]] = parts[1].strip()
    return res


def main():
    raw = os.environ.get("SSH_ORIGINAL_COMMAND", "").strip()
    if not raw:
        fail("пустая команда")
    try:
        argv = shlex.split(raw)
    except ValueError:
        fail("не удалось разобрать команду")
    action = argv[0]
    arg = argv[1] if len(argv) > 1 else None
    if action not in ACTIONS:
        fail("действие «%s» не разрешено" % action)
    if len(argv) > 2:
        fail("слишком много аргументов")

    cfg, meta = load(CONF), load(META)

    if action == "status":
        rc, st = sh(["mita", "status"])
        rc2, sub = sh(["bash", "-c",
                       "curl -s -o /dev/null -w %%{http_code} -m 5 http://127.0.0.1:%d/"
                       % meta["subPort"]])
        out(ok=True, running=("RUNNING" in st), host=meta["host"],
            portRange=portspec(cfg), mtu=cfg.get("mtu"),
            users=len(cfg["users"]), tcpSockets=sockets("t"), udpSockets=sockets("u"),
            connections=connections(), subPort=meta["subPort"],
            subServing=sub.strip() in ("404", "200"), traffic=traffic(),
            lastActive=mita_users())

    if action == "users":
        la, tr = mita_users(), traffic()
        lm, sm = links_map("link"), links_map("sub")
        out(ok=True, host=meta["host"], users=[
            {"name": u["name"], "lastActive": la.get(u["name"]),
             "down": tr.get(u["name"], {}).get("DownloadBytes", 0),
             "up": tr.get(u["name"], {}).get("UploadBytes", 0),
             "link": lm.get(u["name"]), "sub": sm.get(u["name"])}
            for u in cfg["users"]])

    if action in ("link", "sub"):
        if not arg or not NAME_RE.match(arg):
            fail("недопустимое имя")
        if not any(u["name"] == arg for u in cfg["users"]):
            fail("нет пользователя «%s»" % arg)
        out(ok=True, name=arg, value=links_map(action).get(arg))

    if action == "add":
        if not arg or not NAME_RE.match(arg):
            fail("имя должно быть из латиницы, цифр, _ и -, до 32 символов")
        if any(u["name"] == arg for u in cfg["users"]):
            fail("пользователь «%s» уже есть" % arg)
        rc, o = sh(["mieru-user", "add", arg], timeout=60)
        if rc != 0:
            fail("не удалось создать: %s" % o.strip()[:200])
        out(ok=True, name=arg, link=links_map("link").get(arg),
            sub=links_map("sub").get(arg), total=len(cfg["users"]) + 1)

    if action == "del":
        if not arg or not NAME_RE.match(arg):
            fail("недопустимое имя")
        if not any(u["name"] == arg for u in cfg["users"]):
            fail("нет пользователя «%s»" % arg)
        rc, o = sh(["mieru-user", "del", arg], timeout=60)
        if rc != 0:
            fail("не удалось удалить: %s" % o.strip()[:200])
        out(ok=True, name=arg, left=len(cfg["users"]) - 1)

    if action == "server":
        if not arg or not HOST_RE.match(arg):
            fail("недопустимый адрес")
        old = meta["host"]
        rc, o = sh(["mieru-user", "server", arg], timeout=60)
        if rc != 0:
            fail("не удалось сменить адрес: %s" % o.strip()[:200])
        out(ok=True, old=old, new=arg, regenerated=len(meta["tokens"]))


try:
    main()
except Exception as e:
    fail("%s: %s" % (type(e).__name__, e))
