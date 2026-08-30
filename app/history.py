"""SQLite-backed sample history and event log."""
import os
import sqlite3
import threading
import time

DB_PATH = os.environ.get("DB_PATH", "/data/history.db")


class History:
    def __init__(self, path=DB_PATH):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS samples (
                    ts INTEGER NOT NULL,
                    control REAL, cpu1 REAL, cpu2 REAL, inlet REAL, exhaust REAL,
                    fan_pct REAL, target_pct REAL, rpm REAL, power REAL,
                    mode TEXT, emergency INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts);
                CREATE TABLE IF NOT EXISTS events (
                    ts INTEGER NOT NULL, level TEXT, message TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
            """)
            cols = [r[1] for r in self._db.execute("PRAGMA table_info(samples)")]
            if "drive_max" not in cols:  # migration for pre-drive-temp databases
                self._db.execute("ALTER TABLE samples ADD COLUMN drive_max REAL")
            self._db.commit()

    def add_sample(self, **kw):
        with self._lock:
            self._db.execute(
                """INSERT INTO samples
                   (ts, control, cpu1, cpu2, inlet, exhaust, fan_pct,
                    target_pct, rpm, power, mode, emergency, drive_max)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (int(time.time()), kw.get("control"), kw.get("cpu1"),
                 kw.get("cpu2"), kw.get("inlet"), kw.get("exhaust"),
                 kw.get("fan_pct"), kw.get("target_pct"), kw.get("rpm"),
                 kw.get("power"), kw.get("mode"), int(kw.get("emergency", 0)),
                 kw.get("drive_max")))
            self._db.commit()

    def add_event(self, level, message):
        with self._lock:
            self._db.execute("INSERT INTO events (ts, level, message) VALUES (?,?,?)",
                             (int(time.time()), level, message))
            self._db.commit()

    def query(self, seconds, points=400):
        since = int(time.time()) - int(seconds)
        bucket = max(1, int(seconds) // max(10, int(points)))
        with self._lock:
            rows = self._db.execute(
                """SELECT (ts/?)*? AS t,
                          avg(control), avg(cpu1), avg(cpu2), avg(inlet),
                          avg(exhaust), avg(fan_pct), avg(target_pct),
                          avg(rpm), avg(power), max(emergency), avg(drive_max)
                   FROM samples WHERE ts >= ? GROUP BY t ORDER BY t""",
                (bucket, bucket, since)).fetchall()
        keys = ("ts", "control", "cpu1", "cpu2", "inlet", "exhaust",
                "fan_pct", "target_pct", "rpm", "power", "emergency", "drive_max")
        return [dict(zip(keys, r)) for r in rows]

    def events(self, limit=200):
        with self._lock:
            rows = self._db.execute(
                "SELECT ts, level, message FROM events ORDER BY ts DESC, rowid DESC LIMIT ?",
                (int(limit),)).fetchall()
        return [{"ts": r[0], "level": r[1], "message": r[2]} for r in rows]

    def prune(self, retention_days):
        cutoff = int(time.time()) - retention_days * 86400
        with self._lock:
            self._db.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
            self._db.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
            self._db.commit()
