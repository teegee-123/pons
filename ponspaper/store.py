"""Persistence: plain files locally, Postgres when DATABASE_URL is set (e.g. on Render, whose free disk is wiped).

The Postgres store buffers trades and snapshot recordings in memory and writes them together with the state
every PONS_DB_SAVE_SEC seconds (default 600), so a free serverless database isn't kept awake by constant writes.
Table names start with the venue (pons_*, pump_*), so a pons and a pump.fun service can share one database.
Nothing is ever deleted automatically: usage() reports what the data takes and clean() deletes old recordings or
trades when asked to (the dashboard's Database card). If the database refuses writes (e.g. a full free database),
unsaved data is kept in memory only up to a cap.
"""
import csv
import glob
import gzip
import io
import json
import os
import threading
import time
from datetime import datetime

from . import venue as V

MAX_PENDING_SNAPS = 5000    # recording lines held while the database refuses writes (a few hours)
MAX_PENDING_TRADES = 20000
TRADE_FIELDS = ["exit_time", "strategy_id", "strategy", "kind", "symbol", "address", "entry_time", "hold_min",
                "cost_usd", "proceeds_usd", "pnl_usd", "ret_pct", "reason", "entry_drift_pct", "fill_src"] + V.TRADE_FEAT_COLS


def _local_ts(s):
    """trades.csv exit_time (local time) -> unix time; unreadable values count as new, so they are kept."""
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").timestamp()
    except ValueError:
        return float("inf")


def open_store(data_dir):
    url = os.environ.get("DATABASE_URL")
    if url:
        return PgStore(url, data_dir, V.TABLE_PREFIX)
    return FileStore(data_dir)


class FileStore:
    kind = "files"
    save_every = 60.0

    def __init__(self, data_dir):
        self.dir = data_dir
        self._trades_lock = threading.Lock()  # clean() rewrites trades.csv while the engine appends to it
        os.makedirs(data_dir, exist_ok=True)

    def _p(self, name):
        return os.path.join(self.dir, name)

    @staticmethod
    def _atomic_write(path, text):
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf8") as fh:
                fh.write(text)
            os.replace(tmp, path)
        except OSError:
            pass

    def load_config(self):
        return self._load_json("config.json")

    def save_config(self, cfg):
        self._atomic_write(self._p("config.json"), json.dumps(cfg, indent=2))

    def load_state(self):
        self.loaded_at = self.state_updated()
        return self._load_json("state.json")

    def state_updated(self):
        """When the saved state was last written (unix time), or None."""
        try:
            return os.path.getmtime(self._p("state.json"))
        except OSError:
            return None

    def save_state(self, text):
        self._atomic_write(self._p("state.json"), text)

    def _load_json(self, name):
        try:
            with open(self._p(name), encoding="utf8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    def append_trade(self, row):
        path = self._p("trades.csv")
        with self._trades_lock:
            new = not os.path.exists(path)
            try:
                with open(path, "a", newline="", encoding="utf8") as fh:
                    w = csv.writer(fh)
                    if new:
                        w.writerow(TRADE_FIELDS)
                    w.writerow(row)
            except OSError:
                pass

    def append_snapshots(self, lines):
        d = self._p("snapshots")
        os.makedirs(d, exist_ok=True)
        name = os.path.join(d, datetime.now().strftime("%Y-%m-%d_%H") + ".jsonl.gz")
        try:
            with gzip.open(name, "at", encoding="utf8") as fh:
                fh.write("\n".join(lines) + "\n")
        except OSError:
            pass

    def flush(self):
        pass

    def trades_csv(self):
        try:
            with open(self._p("trades.csv"), "rb") as fh:
                return fh.read()
        except OSError:
            return (",".join(TRADE_FIELDS) + "\n").encode()

    def snapshot_lines(self, since):
        """Recorded lines from hourly files that may contain data newer than `since` (unix time)."""
        cut = datetime.fromtimestamp(since).strftime("%Y-%m-%d_%H")
        for path in sorted(glob.glob(self._p(os.path.join("snapshots", "*.jsonl.gz")))):
            if os.path.basename(path)[:13] < cut:
                continue
            try:
                with gzip.open(path, "rt", encoding="utf8") as fh:
                    yield from fh
            except (OSError, EOFError):
                continue

    def snapshots_gz(self):
        """All recordings as one multi-member gzip stream (readable by `replay`)."""
        out = io.BytesIO()
        for path in sorted(glob.glob(self._p(os.path.join("snapshots", "*.jsonl.gz")))):
            with open(path, "rb") as fh:
                out.write(fh.read())
        return out.getvalue()

    def _snapshot_files(self):
        """[(hour start as unix time, path)] of the hourly recording files, oldest first."""
        out = []
        for path in sorted(glob.glob(self._p(os.path.join("snapshots", "*.jsonl.gz")))):
            try:
                out.append((datetime.strptime(os.path.basename(path)[:13], "%Y-%m-%d_%H").timestamp(), path))
            except ValueError:
                continue
        return out

    def usage(self):
        files = self._snapshot_files()
        size = lambda p: os.path.getsize(p) if os.path.exists(p) else 0
        rec = sum(size(p) for _, p in files)
        trades, state = size(self._p("trades.csv")), size(self._p("state.json"))
        return {"kind": self.kind, "databaseBytes": rec + trades + state,
                "recordings": {"bytes": rec, "rows": len(files), "unit": "hourly files",
                               "from": files[0][0] if files else None, "to": files[-1][0] + 3600 if files else None},
                "trades": {"bytes": trades, "rows": None, "from": None, "to": None},
                "state": {"bytes": state}, "pending": {"recordings": 0, "trades": 0}}

    def clean(self, rec_days=None, trade_days=None):
        """Delete hourly recording files and trades.csv rows older than the given number of days."""
        now, done = time.time(), {}
        if rec_days is not None:
            old = [p for t, p in self._snapshot_files() if t + 3600 <= now - rec_days * 86400]
            for p in old:
                try:
                    os.remove(p)
                except OSError:
                    pass
            done["recordings"] = len(old)
        if trade_days is not None:
            path, cut = self._p("trades.csv"), now - trade_days * 86400
            with self._trades_lock:
                try:
                    with open(path, newline="", encoding="utf8") as fh:
                        rows = list(csv.reader(fh))
                except OSError:
                    rows = []
                if rows:
                    keep = [r for r in rows[1:] if r and _local_ts(r[0]) >= cut]
                    with open(path, "w", newline="", encoding="utf8") as fh:
                        csv.writer(fh).writerows([rows[0]] + keep)
                    done["trades"] = len(rows) - 1 - len(keep)
        return {"deleted": done, "compacted": True}


class PgStore:
    kind = "postgres"

    def __init__(self, url, data_dir, prefix="pons"):
        import psycopg  # only needed when DATABASE_URL is set (see requirements.txt)
        self._psycopg = psycopg
        self.url = url
        self.dir = data_dir
        self.kv, self.tr, self.sn = f"{prefix}_kv", f"{prefix}_trades", f"{prefix}_snapshots"
        self.save_every = float(os.environ.get("PONS_DB_SAVE_SEC", "600"))
        self._lock = threading.Lock()
        self._db_lock = threading.RLock()
        self._trades = []
        self._snaps = []
        self._state = None
        self._conn = None
        self.last_error = None
        with self._db() as c:
            c.execute(f"create table if not exists {self.kv} (k text primary key, v text not null, updated timestamptz default now())")
            c.execute(f"create table if not exists {self.tr} (id bigserial primary key, t timestamptz default now(), row jsonb not null)")
            c.execute(f"create table if not exists {self.sn} (id bigserial primary key, t timestamptz default now(), data bytea not null)")

    def _db(self):
        if self._conn is None or self._conn.closed:
            self._conn = self._psycopg.connect(self.url, autocommit=True, connect_timeout=15)
        return self._conn.cursor()

    def _run(self, fn):
        with self._db_lock:  # one connection shared by the poller, the lab and HTTP threads
            return self._run_locked(fn)

    def _run_locked(self, fn):
        for attempt in range(2):
            try:
                with self._db() as c:
                    return fn(c)
            except self._psycopg.OperationalError as e:  # dropped connection / db woke up: reconnect once
                self.last_error = repr(e)
                self._conn = None
                if attempt:
                    raise

    def _get(self, k):
        row = self._run(lambda c: (c.execute(f"select v from {self.kv} where k=%s", (k,)), c.fetchone())[1])
        return json.loads(row[0]) if row else None

    def _put(self, c, k, v):
        c.execute(f"insert into {self.kv} (k, v) values (%s, %s) on conflict (k) do update set v=excluded.v, updated=now()", (k, v))

    def load_config(self):
        return self._get("config")

    def save_config(self, cfg):
        self._run(lambda c: self._put(c, "config", json.dumps(cfg)))

    def load_state(self):
        row = self._run(lambda c: (c.execute(f"select v, extract(epoch from updated) from {self.kv} where k='state'"),
                                   c.fetchone())[1])
        self.loaded_at = float(row[1]) if row else None
        return json.loads(row[0]) if row else None

    def state_updated(self):
        """When the saved state was last written (unix time), or None."""
        row = self._run(lambda c: (c.execute(f"select extract(epoch from updated) from {self.kv} where k='state'"),
                                   c.fetchone())[1])
        return float(row[0]) if row else None

    def save_state(self, text):
        with self._lock:
            self._state = text  # written by flush()

    def append_trade(self, row):
        with self._lock:
            self._trades.append(dict(zip(TRADE_FIELDS, row)))

    def append_snapshots(self, lines):
        with self._lock:
            self._snaps.extend(lines)

    def flush(self):
        with self._lock:
            state, trades, snaps = self._state, self._trades, self._snaps
            self._state, self._trades, self._snaps = None, [], []
        if state is None and not trades and not snaps:
            return
        blob = gzip.compress(("\n".join(snaps) + "\n").encode()) if snaps else None

        def write(c):
            if state is not None:
                self._put(c, "state", state)
            for r in trades:
                c.execute(f"insert into {self.tr} (row) values (%s::jsonb)", (json.dumps(r),))
            if blob:
                c.execute(f"insert into {self.sn} (data) values (%s)", (blob,))
        try:
            self._run(write)
            self.last_error = None
        except Exception as e:  # keep the data for the next attempt
            self.last_error = repr(e)
            with self._lock:
                if self._state is None:
                    self._state = state
                # never grow without bound: a full or unreachable database must not exhaust memory
                self._trades = (trades + self._trades)[-MAX_PENDING_TRADES:]
                self._snaps = (snaps + self._snaps)[-MAX_PENDING_SNAPS:]

    def trades_csv(self):
        rows = self._run(lambda c: (c.execute(f"select row from {self.tr} order by id"), c.fetchall())[1]) or []
        with self._lock:
            pending = list(self._trades)
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(TRADE_FIELDS)
        for (r,) in rows:
            r = r if isinstance(r, dict) else json.loads(r)
            w.writerow([r.get(k, "") for k in TRADE_FIELDS])
        for r in pending:
            w.writerow([r.get(k, "") for k in TRADE_FIELDS])
        return out.getvalue().encode()

    def snapshot_lines(self, since):
        """Recorded lines newer than roughly `since`, chunk by chunk, then whatever is still buffered."""
        ids = self._run(lambda c: (c.execute(f"select id from {self.sn} where t >= to_timestamp(%s) order by id",
                                             (since - self.save_every,)), c.fetchall())[1]) or []
        for (i,) in ids:
            row = self._run(lambda c: (c.execute(f"select data from {self.sn} where id=%s", (i,)), c.fetchone())[1])
            if row:
                yield from gzip.decompress(bytes(row[0])).decode().splitlines()
        with self._lock:
            pending = list(self._snaps)
        yield from pending

    def usage(self):
        """What the database holds: total size, and size / rows / time span of recordings and trades."""
        def q(c):
            c.execute("select pg_database_size(current_database())")
            out = {"kind": self.kind, "databaseBytes": c.fetchone()[0]}
            for name, table in (("recordings", self.sn), ("trades", self.tr)):
                c.execute(f"select pg_total_relation_size(%s::regclass), count(*), extract(epoch from min(t)), "
                          f"extract(epoch from max(t)) from {table}", (table,))
                size, rows, t0, t1 = c.fetchone()
                out[name] = {"bytes": size, "rows": rows, "unit": "chunks" if name == "recordings" else "trades",
                             "from": float(t0) if t0 is not None else None, "to": float(t1) if t1 is not None else None}
            c.execute("select pg_total_relation_size(%s::regclass)", (self.kv,))
            out["state"] = {"bytes": c.fetchone()[0]}
            return out
        out = self._run(q)
        with self._lock:
            out["pending"] = {"recordings": len(self._snaps), "trades": len(self._trades)}
        return out

    def clean(self, rec_days=None, trade_days=None):
        """Delete recordings / stored trades older than the given number of days, then rewrite the tables so the
        space is handed back (Postgres keeps a table's size after a delete until it is rewritten)."""
        cut = time.time()
        done = {}

        def delete(c):
            for name, table, days in (("recordings", self.sn, rec_days), ("trades", self.tr, trade_days)):
                if days is not None:
                    c.execute(f"delete from {table} where t < to_timestamp(%s)", (cut - days * 86400,))
                    done[name] = c.rowcount
        self._run(delete)
        compacted = True
        for name, table in (("recordings", self.sn), ("trades", self.tr)):
            if not done.get(name):
                continue
            try:  # needs room for a copy of what is kept; a database at its size limit may refuse
                self._run(lambda c: c.execute(f"vacuum full {table}"))
            except Exception:
                compacted = False
                try:  # the freed space is at least reused by new writes, so the database stops growing
                    self._run(lambda c: c.execute(f"vacuum {table}"))
                except Exception:
                    pass
        return {"deleted": done, "compacted": compacted}

    def snapshots_gz(self):
        rows = self._run(lambda c: (c.execute(f"select data from {self.sn} order by id"), c.fetchall())[1]) or []
        with self._lock:
            pending = list(self._snaps)
        out = io.BytesIO()
        for (b,) in rows:
            out.write(bytes(b))
        if pending:
            out.write(gzip.compress(("\n".join(pending) + "\n").encode()))
        return out.getvalue()
