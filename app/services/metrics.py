"""История метрик сервера в SQLite для /graph.

Все функции синхронные — вызывать через asyncio.to_thread.
База лежит в data/ (проброшено volume-ом), переживает пересборку образа.
"""

import sqlite3
import time
from pathlib import Path

from app.config import settings


def _connect() -> sqlite3.Connection:
    path = Path(settings.metrics_db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS metrics ("
        "ts INTEGER PRIMARY KEY, cpu REAL, ram REAL, temp REAL)"
    )
    # Базовые уровни накопительных SMART-счётчиков: алертим только при росте
    conn.execute(
        "CREATE TABLE IF NOT EXISTS smart_baseline ("
        "device TEXT, attr TEXT, value INTEGER, PRIMARY KEY (device, attr))"
    )
    return conn


def get_smart_baseline(device: str, attr: str) -> int | None:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT value FROM smart_baseline WHERE device = ? AND attr = ?",
            (device, attr),
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def set_smart_baseline(device: str, attr: str, value: int) -> None:
    conn = _connect()
    try:
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO smart_baseline (device, attr, value) VALUES (?, ?, ?)",
                (device, attr, value),
            )
    finally:
        conn.close()


def record(cpu: float, ram: float, temp: float | None) -> None:
    now = int(time.time())
    cutoff = now - settings.metrics_retention_days * 86400
    conn = _connect()
    try:
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO metrics (ts, cpu, ram, temp) VALUES (?, ?, ?, ?)",
                (now, cpu, ram, temp),
            )
            conn.execute("DELETE FROM metrics WHERE ts < ?", (cutoff,))
    finally:
        conn.close()


def fetch(since_ts: int) -> list[tuple[int, float, float, float | None]]:
    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT ts, cpu, ram, temp FROM metrics WHERE ts >= ? ORDER BY ts",
            (since_ts,),
        )
        return cur.fetchall()
    finally:
        conn.close()


def last_ts() -> int | None:
    """Время последней записанной точки метрик.

    Нужно, чтобы понять, когда сервер был жив в последний раз. Детектор
    «выключили свет» по аккумулятору на этой машине не работает — батареи
    нет, и при пропаже питания она гаснет молча. Остаётся узнавать о
    простое задним числом, сравнивая эту отметку с моментом загрузки.
    """
    conn = _connect()
    try:
        row = conn.execute("SELECT MAX(ts) FROM metrics").fetchone()
        return row[0] if row and row[0] else None
    finally:
        conn.close()
