"""Проверка UDP через socks5: DNS-запросы через UDP ASSOCIATE.

    python3 socks-udp-test.py HOST PORT [N]

Например, мост телевизоров: python3 socks-udp-test.py 127.0.0.1 1081

Шлёт N запросов A youtube.com на 1.1.1.1:53 через прокси. Настоящий адрес
в ответе — UDP ушёл через VPN; NXDOMAIN — вышел напрямую и провайдер
подменил ответ; тишина — UDP через прокси не проходит.
"""
import random, socket, struct, sys, time

host, port = sys.argv[1], int(sys.argv[2])
n = int(sys.argv[3]) if len(sys.argv) > 3 else 20

def dns_query(qid):
    q = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)
    for part in b"youtube.com".split(b"."):
        q += bytes([len(part)]) + part
    return q + b"\x00" + struct.pack(">HH", 1, 1)

def parse(resp, qid):
    rid, flags, qd, an = struct.unpack(">HHHH", resp[:8])
    if rid != qid:
        return "чужой ответ"
    rcode = flags & 0xF
    if rcode == 3:
        return "NXDOMAIN"
    if rcode:
        return "rcode=%d" % rcode
    # пропускаем вопрос и берём первый A из ответа
    i = 12
    while resp[i]:
        i += resp[i] + 1
    i += 5
    for _ in range(an):
        if resp[i] & 0xC0 == 0xC0:
            i += 2
        else:
            while resp[i]:
                i += resp[i] + 1
            i += 1
        typ, _cls, _ttl, rdlen = struct.unpack(">HHIH", resp[i:i + 10])
        i += 10
        if typ == 1 and rdlen == 4:
            return socket.inet_ntoa(resp[i:i + 4])
        i += rdlen
    return "без A-записи"

tcp = socket.create_connection((host, port), timeout=5)
tcp.sendall(b"\x05\x01\x00")
assert tcp.recv(2) == b"\x05\x00", "прокси не принял рукопожатие"
tcp.sendall(b"\x05\x03\x00\x01" + b"\x00" * 6)
rep = tcp.recv(10)
assert rep[1] == 0, "UDP ASSOCIATE отклонён, код %d" % rep[1]
relay = (socket.inet_ntoa(rep[4:8]), struct.unpack(">H", rep[8:10])[0])
if relay[0] == "0.0.0.0":
    relay = (host, relay[1])

udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
udp.settimeout(3)
hdr = b"\x00\x00\x00\x01" + socket.inet_aton("1.1.1.1") + struct.pack(">H", 53)
results, rtts = {}, []
for _ in range(n):
    qid = random.randrange(65536)
    t = time.time()
    udp.sendto(hdr + dns_query(qid), relay)
    try:
        data, _ = udp.recvfrom(4096)
        rtts.append((time.time() - t) * 1000)
        r = parse(data[10:], qid)
    except socket.timeout:
        r = "тишина"
    results[r if r in ("NXDOMAIN", "тишина") or not r[0].isdigit() else "настоящий адрес"] = \
        results.get(r if r in ("NXDOMAIN", "тишина") or not r[0].isdigit() else "настоящий адрес", 0) + 1
print("релей", relay, "| ответы:", results,
      "| задержка мс: мин %.0f, средн %.0f, макс %.0f" % (min(rtts), sum(rtts) / len(rtts), max(rtts)) if rtts else "")
