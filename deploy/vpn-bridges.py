#!/usr/bin/env python3
"""Домашние мосты mieru: бот (homepilot-mieru) и телевизоры (tv-vpn-mieru).

    sudo python3 vpn-bridges.py https://sub.example.ru/sub [--refresh]
    sudo python3 vpn-bridges.py --probe НОВЫЙ_IP

Мосты — обычные клиенты по подписке: адреса VPS в их конфигах нет, его
приносит подписка. Чтобы это пережило переезд, делается две вещи.

1. Ссылка на подписку переводится на постоянный домен (зеркало на этом же
   сервере). Раньше мосты ходили прямо на VPS — http://IP:8080/ТОКЕН — и
   после переезда продолжили бы качать старый адрес со старого сервера, а
   когда тот умрёт, остались бы на кэше с мёртвым адресом. Бот при этом
   теряет Telegram, телевизоры — туннель.

2. --refresh кладёт в кэш моста свежую копию из зеркала и перезапускает
   контейнер. Без этого mihomo дождался бы своего интервала (до часа), а
   если старый сервер уже лежит — это час без Telegram. Кэш свежее
   интервала mihomo при старте не перекачивает, поэтому подменять надо
   именно его.

Токены на экран не выводятся: скрипт работает с ними только внутри себя.
"""
import os, re, shutil, subprocess, sys, time

BRIDGES = [
    # контейнер,        конфиг,                                 socks-порт на хосте
    ("homepilot-mieru", "/home/san/Tg_bot/mieru/config.yaml", 1080),
    ("tv-vpn-mieru",    "/home/san/tv-vpn/mieru.yaml",        1081),
]
MIRROR = "/home/san/vpn-mirror/www/sub"

URL_RE = re.compile(r'^(?P<pre>[ \t]*url:[ \t]*)(?P<q>"?)(?P<url>https?://[^"\s]+)(?P=q)[ \t]*$', re.M)
PATH_RE = re.compile(r'^[ \t]*path:[ \t]*"?([^"\s]+)"?[ \t]*$', re.M)
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{20,}$")


def ok(msg):
    print("   ✓ " + msg)


def bad(msg):
    print("   ✗ " + msg)


def provider_url(text):
    """Ссылка на подписку — единственный url, оканчивающийся токеном.

    Остальные url в конфиге — проверки живости (cp.cloudflare.com), их не трогаем.
    """
    found = [m for m in URL_RE.finditer(text)
             if TOKEN_RE.match(m.group("url").rstrip("/").rsplit("/", 1)[-1])]
    if len(found) != 1:
        raise ValueError("ожидалась ровно одна ссылка на подписку, нашлось %d" % len(found))
    return found[0]


def write_in_place(path, text):
    # Конфиг примонтирован в контейнер одиночным файлом. Замена через
    # rename дала бы новый inode, а контейнер видел бы старый — пишем
    # поверх того же файла.
    with open(path, "r+") as f:
        f.seek(0)
        f.write(text)
        f.truncate()


def check_socks(port, tries=6):
    for _ in range(tries):
        r = subprocess.run(
            ["curl", "-s", "-m", "10", "-o", "/dev/null", "-w", "%{http_code}",
             "--socks5-hostname", "127.0.0.1:%d" % port, "https://api.telegram.org/"],
            capture_output=True, text=True)
        code = r.stdout.strip()
        if code and code != "000":
            return code
        time.sleep(5)
    return None


def handle(container, config, port, sub_base, refresh):
    print("\n  %s" % container)
    if not os.path.isfile(config):
        bad("нет конфига %s — пропускаю" % config)
        return True  # моста на этой машине нет, это не ошибка

    text = open(config).read()
    m = provider_url(text)
    token = m.group("url").rstrip("/").rsplit("/", 1)[-1]
    want = "%s/%s" % (sub_base.rstrip("/"), token)

    changed = m.group("url") != want
    if changed:
        backup = "%s.bak-%s" % (config, time.strftime("%Y%m%d-%H%M%S"))
        shutil.copy2(config, backup)
        new = text[:m.start()] + '%s"%s"' % (m.group("pre"), want) + text[m.end():]
        write_in_place(config, new)
        ok("подписка переведена на постоянный адрес (копия: %s)" % os.path.basename(backup))
    else:
        ok("подписка уже на постоянном адресе")

    if not (refresh or changed):
        return True

    pm = PATH_RE.search(text)
    cache = os.path.normpath(os.path.join(os.path.dirname(config), pm.group(1))) if pm else None
    src = os.path.join(MIRROR, token)
    fresh = cache and os.path.isfile(src) and "proxies:" in open(src).read()
    if fresh:
        st = os.stat(cache) if os.path.exists(cache) else None
        shutil.copyfile(src, cache)
        if st:
            os.chown(cache, st.st_uid, st.st_gid)
        ok("кэш обновлён из зеркала")
    else:
        bad("в зеркале нет копии для этого моста — подтянет сам в течение часа")

    subprocess.run(["docker", "restart", container], check=True, stdout=subprocess.DEVNULL)
    code = check_socks(port)
    if code:
        ok("перезапущен, Telegram через него отвечает (HTTP %s)" % code)
        return True
    bad("перезапущен, но Telegram через 127.0.0.1:%d не отвечает" % port)
    return False


PROBE_NAME = "vps-migrate-probe"
PROBE_PORT = 17890


def probe(host, sub_port=8080):
    """Сквозная проверка нового сервера ДО переключения.

    Берёт подписку моста бота прямо с нового VPS, поднимает рядом временный
    mihomo с ней и открывает через него Telegram. Проверяет всё сразу: mita
    знает пользователей со старыми паролями, порты открыты, раздача на 8080
    отдаёт подписку с новым адресом. Если здесь не прошло — переключать дом
    нельзя, иначе бот потеряет Telegram.
    """
    import tempfile, urllib.request
    text = open(BRIDGES[0][1]).read()
    token = provider_url(text).group("url").rstrip("/").rsplit("/", 1)[-1]
    with urllib.request.urlopen("http://%s:%d/%s" % (host, sub_port, token), timeout=15) as r:
        sub = r.read().decode()
    if "server: %s" % host not in sub:
        bad("подписка на новом сервере указывает не на %s" % host)
        return 1
    ok("новый сервер отдаёт подписку с адресом %s" % host)

    work = tempfile.mkdtemp(prefix="vps-probe-")  # 700: внутри будет пароль
    try:
        with open(os.path.join(work, "config.yaml"), "w") as f:
            f.write("mixed-port: %d\nallow-lan: true\nbind-address: '*'\n"
                    "mode: rule\nlog-level: warning\nipv6: false\n" % PROBE_PORT)
            f.write(sub)
        subprocess.run(["docker", "rm", "-f", PROBE_NAME], capture_output=True)
        subprocess.run(
            ["docker", "run", "-d", "--rm", "--name", PROBE_NAME,
             "-p", "127.0.0.1:%d:%d" % (PROBE_PORT, PROBE_PORT),
             "-v", "%s:/root/.config/mihomo" % work, "metacubex/mihomo:latest"],
            check=True, stdout=subprocess.DEVNULL)
        time.sleep(3)
        code = check_socks(PROBE_PORT)
    finally:
        subprocess.run(["docker", "rm", "-f", PROBE_NAME], capture_output=True)
        shutil.rmtree(work, ignore_errors=True)
    if code:
        ok("через новый сервер открывается Telegram (HTTP %s)" % code)
        return 0
    bad("через новый сервер Telegram не открывается")
    return 1


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if "--probe" in sys.argv and len(args) == 1:
        try:
            return probe(args[0])
        except Exception as e:
            bad("%s: %s" % (type(e).__name__, e))
            return 1
    if len(args) != 1 or not args[0].startswith("https://"):
        print("\n".join(__doc__.strip().splitlines()[2:4]))
        return 2
    refresh = "--refresh" in sys.argv
    good = True
    for container, config, port in BRIDGES:
        try:
            good &= handle(container, config, port, args[0], refresh)
        except Exception as e:  # один сломанный мост не должен мешать второму
            bad("%s: %s" % (type(e).__name__, e))
            good = False
    return 0 if good else 1


if __name__ == "__main__":
    sys.exit(main())
