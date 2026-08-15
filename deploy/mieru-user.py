#!/usr/bin/env python3
"""Управление пользователями mieru + раздача подписок.

Источник правды по юзерам - /root/mieru-server.json (его же жуёт mita).
ВАЖНО: mita apply config затирает список users целиком, поэтому всегда
пишем полный файл, а не патч.

Адрес сервера и токены подписок - /root/mieru-meta.json. Смена адреса
(команда server) перегенерирует все файлы подписок, и клиенты подтянут
новый сервер сами - обходить людей руками не нужно.
"""
import hashlib, json, sys, os, subprocess, secrets, string

CONF  = "/root/mieru-server.json"
META  = "/root/mieru-meta.json"
SUBD  = "/opt/mieru-sub"
ALPHA = string.ascii_letters + string.digits


def load(p):
    with open(p) as f:
        return json.load(f)


def save(path, data, mode=0o600):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def apply_cfg(cfg):
    save(CONF, cfg)
    subprocess.run(["mita", "apply", "config", CONF], check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["mita", "reload"], check=False, stdout=subprocess.DEVNULL)


def portspec(cfg):
    for pb in cfg.get("portBindings", []):
        if pb.get("protocol") == "TCP":
            return pb.get("portRange") or str(pb.get("port"))
    raise SystemExit("в конфиге нет TCP-биндинга")


def link(cfg, meta, name, pw):
    return ("mierus://%s:%s@%s?mtu=%d&port=%s&profile=%s&protocol=TCP"
            % (name, pw, meta["host"], cfg.get("mtu", 1280),
               portspec(cfg), meta["profile"]))


def suburl(meta, token):
    """Ссылка, которую получает человек.

    Если в мете задан publicUrl - постоянный адрес раздачи, живущий отдельно
    от VPN-сервера, - выдаём его. Смысл в переезде: адрес VPS меняется, а
    ссылка у человека остаётся прежней, и обходить никого не нужно. Прямая
    ссылка на VPS остаётся запасным вариантом, когда publicUrl не задан.
    """
    base = meta.get("publicUrl")
    if base:
        return "%s/%s" % (base.rstrip("/"), token)
    return "http://%s:%d/%s" % (meta["host"], meta["subPort"], token)


PORTS_PER_USER = 6


def user_ports(cfg, name, count=PORTS_PER_USER):
    """Свой набор портов на каждого, выведенный из имени.

    Детерминированно: при перегенерации подписки набор тот же, и клиенту не
    приходится заново перемерять узлы. Плюс заблокированные порты одного
    человека не задевают остальных.
    """
    spec = portspec(cfg)
    lo, _, hi = spec.partition("-")
    lo, hi = int(lo), int(hi or lo)
    span = hi - lo + 1
    if span <= count:
        return list(range(lo, hi + 1))
    picked = []
    salt = 0
    while len(picked) < count and salt < 500:
        digest = hashlib.sha256(("%s:%d" % (name, salt)).encode()).digest()
        port = lo + int.from_bytes(digest[:4], "big") % span
        if port not in picked:
            picked.append(port)
        salt += 1
    return sorted(picked)


def write_sub(cfg, meta, name, pw, token):
    """Подписка в формате Clash YAML: несколько узлов на одиночных портах.

    Два отказа от очевидных решений, оба выстраданы на живом клиенте:

    1. Не base64-список mierus://. Эту схему разбирают не все клиенты: в
       Karing она появилась в 1.2.23, а в App Store до сих пор 1.2.22 — на
       айфон новее не поставить.
    2. Не port-range одной строкой. Ядро mihomo внутри iOS-сборки это поле
       не понимает и молча выбрасывает такой прокси, клиент остаётся с
       пустым списком и руганью "No server available".

    Одиночные порты понимают все версии, а размазывание по портам никуда не
    делось: узлов несколько, и группа fallback сама уйдёт на следующий порт,
    если текущий заблокируют.
    """
    nl = chr(10)
    ports = user_ports(cfg, name)
    profile = meta["profile"]
    lines = ["proxies:"]
    for idx, port in enumerate(ports, 1):
        lines += [
            '  - name: "%s-%d"' % (profile, idx),
            "    type: mieru",
            "    server: %s" % meta["host"],
            "    port: %d" % port,
            "    transport: TCP",
            '    username: "%s"' % name,
            '    password: "%s"' % pw,
            # 0-RTT рукопожатие экономит круг до сервера на каждом новом
            # соединении. Страница открывает их десятками, поэтому на
            # ощущениях от браузера это заметнее, чем мегабиты.
            # Замер задержки: 407 мс -> 301 мс на запрос.
            "    handshake-mode: HANDSHAKE_NO_WAIT",
            "    multiplexing: MULTIPLEXING_HIGH",
            "",
        ]
    lines += [
        "proxy-groups:",
        '  - name: "VPN"',
        "    type: fallback",
        "    url: http://cp.cloudflare.com/generate_204",
        "    interval: 300",
        "    proxies:",
    ]
    lines += ['      - "%s-%d"' % (profile, i) for i in range(1, len(ports) + 1)]
    lines += ["", "rules:", "  - MATCH,VPN", ""]

    path = os.path.join(SUBD, token)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(nl.join(lines))
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def find(cfg, name):
    for u in cfg["users"]:
        if u["name"] == name:
            return u
    return None


def regen_all(cfg, meta):
    for u in cfg["users"]:
        tok = meta["tokens"].get(u["name"])
        if tok:
            write_sub(cfg, meta, u["name"], u["password"], tok)


def main():
    if len(sys.argv) < 2:
        print("usage: mieru-user add|del|link|sub|list|server [АРГУМЕНТ]")
        return 1
    cmd = sys.argv[1]
    arg = sys.argv[2] if len(sys.argv) > 2 else None
    cfg, meta = load(CONF), load(META)

    if cmd == "add":
        if not arg:
            print("usage: mieru-user add ИМЯ"); return 1
        if find(cfg, arg):
            print("пользователь %s уже есть" % arg); return 1
        pw = "".join(secrets.choice(ALPHA) for _ in range(20))
        tok = secrets.token_urlsafe(24)
        cfg["users"].append({"name": arg, "password": pw})
        meta["tokens"][arg] = tok
        apply_cfg(cfg)
        save(META, meta)
        write_sub(cfg, meta, arg, pw, tok)
        print("добавлен %s (всего %d)\n" % (arg, len(cfg["users"])))
        print("подписка (давать людям её):\n  %s\n" % suburl(meta, tok))
        print("прямая ссылка (если клиент не умеет подписки):\n  %s" % link(cfg, meta, arg, pw))

    elif cmd == "del":
        if not arg:
            print("usage: mieru-user del ИМЯ"); return 1
        if not find(cfg, arg):
            print("нет такого: %s" % arg); return 1
        cfg["users"] = [u for u in cfg["users"] if u["name"] != arg]
        tok = meta["tokens"].pop(arg, None)
        apply_cfg(cfg)
        save(META, meta)
        subprocess.run(["mita", "delete", "user", arg], check=False, stdout=subprocess.DEVNULL)
        if tok:
            try:
                os.remove(os.path.join(SUBD, tok))
            except FileNotFoundError:
                pass
        print("удалён %s (осталось %d)" % (arg, len(cfg["users"])))

    elif cmd in ("link", "sub"):
        users = [find(cfg, arg)] if arg else cfg["users"]
        if arg and not users[0]:
            print("нет такого: %s" % arg); return 1
        for u in users:
            val = (link(cfg, meta, u["name"], u["password"]) if cmd == "link"
                   else suburl(meta, meta["tokens"].get(u["name"], "<нет-токена>")))
            print("%-12s %s" % (u["name"], val) if not arg else val)

    elif cmd == "list":
        subprocess.run(["mita", "get", "users"])

    elif cmd == "server":
        if not arg:
            print("текущий адрес: %s" % meta["host"]); return 0
        old = meta["host"]
        meta["host"] = arg
        save(META, meta)
        regen_all(cfg, meta)
        print("адрес сервера: %s -> %s" % (old, arg))
        print("перегенерировано подписок: %d" % len(meta["tokens"]))
        print("клиенты подтянут новый адрес сами при следующем обновлении подписки")

    else:
        print("неизвестная команда: %s" % cmd); return 1
    return 0


sys.exit(main())
