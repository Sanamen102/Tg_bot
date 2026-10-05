#!/usr/bin/env python3
"""Видно ли адрес из России: TCP-проверка с российских узлов check-host.net.

    python3 ru-reach.py АДРЕС ПОРТ [ПОРТ ...]

Коды выхода: 0 — все порты видны со всех узлов; 2 — какой-то порт
не виден ни с одного (адрес или порт режут, переезжать туда нельзя);
3 — виден частично (как было со старым литовским сервером: один узел не
видел его вовсе); 1 — check-host недоступен, проверить не удалось.

Проверка с домашнего сервера тут не годится: он один, а блокировки
у российских операторов разные.
"""
import json, sys, time, urllib.request

API = "https://check-host.net"


def get(path):
    req = urllib.request.Request(API + path, headers={
        "Accept": "application/json", "User-Agent": "vps-migrate"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def check(host, port, nodes):
    q = "&".join("node=" + n for n in nodes)
    rid = get("/check-tcp?host=%s:%d&%s" % (host, port, q))["request_id"]
    res = {}
    for _ in range(15):  # узлы отвечают за 5-20 секунд
        time.sleep(3)
        res = get("/check-result/" + rid)
        if all(res.get(n) is not None for n in nodes):
            break
    # None — узел так и не ответил. Это не «порт закрыт»: считать его
    # непрозрачным значило бы зря остановить переезд (код 2).
    seen = {}
    for n in nodes:
        r = res.get(n)
        seen[n] = None if r is None else (bool(r) and isinstance(r[0], dict) and "time" in r[0])
    return seen


def main():
    if len(sys.argv) < 3:
        print(__doc__.strip().splitlines()[2])
        return 1
    host, ports = sys.argv[1], [int(p) for p in sys.argv[2:]]
    try:
        allnodes = get("/nodes/hosts")["nodes"]
        nodes = sorted(n for n, v in allnodes.items()
                       if str((v.get("location") or [""])[0]).lower() == "ru")
        if not nodes:
            print("   check-host не вернул российских узлов")
            return 1
        worst = 0
        for port in ports:
            seen = check(host, port, nodes)
            answered = {n: v for n, v in seen.items() if v is not None}
            good = sum(answered.values())
            short = " ".join(
                n.split(".")[0] + ("?" if v is None else "+" if v else "-") for n, v in seen.items()
            )
            print("   порт %d: виден с %d из %d ответивших узлов РФ  [%s]"
                  % (port, good, len(answered), short))
            if not answered:
                worst = max(worst, 1)  # проверить не удалось
            elif good == 0:
                worst = 2
            elif good < len(answered) and worst != 2:
                worst = 3
        return worst
    except Exception as e:
        print("   check-host недоступен: %s: %s" % (type(e).__name__, e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
