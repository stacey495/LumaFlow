import hashlib
import hmac
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager

from . import config
from .httpio import HTTPError

FIELDS = "id,owner,name,width,height,media,profile,status,progress,stage,worker,created,started,ended,error"
SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, salt BLOB NOT NULL,
    password BLOB NOT NULL, role TEXT NOT NULL CHECK(role IN ('user','admin'))
);
CREATE TABLE IF NOT EXISTS sessions(
    token BLOB PRIMARY KEY, owner INTEGER NOT NULL REFERENCES users(id),
    csrf TEXT NOT NULL, expires REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs(
    id TEXT PRIMARY KEY, owner INTEGER NOT NULL REFERENCES users(id), name TEXT NOT NULL,
    width INTEGER NOT NULL, height INTEGER NOT NULL, media TEXT NOT NULL, profile TEXT NOT NULL,
    status TEXT NOT NULL, progress INTEGER NOT NULL, stage TEXT NOT NULL, worker TEXT,
    created REAL NOT NULL, started REAL, ended REAL, error TEXT
);
CREATE TABLE IF NOT EXISTS images(
    id TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE, original BLOB NOT NULL, result BLOB
);
CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(status,created);
CREATE INDEX IF NOT EXISTS jobs_owner ON jobs(owner,created);
CREATE INDEX IF NOT EXISTS sessions_expiration ON sessions(expires);
"""


def password_hash(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600_000)


def credentials(body):
    name, password = body.get("username"), body.get("password")
    if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9_]{3,24}", name):
        raise HTTPError(400, "Логін має містити 3–24 латинські літери, цифри або підкреслення.")
    if not isinstance(password, str) or not 8 <= len(password) <= 128:
        raise HTTPError(400, "Пароль має містити 8–128 символів.")
    return name.lower(), password


class Store:
    def __init__(self, path, admin_password):
        self.path = path
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
            if db.execute("SELECT 1 FROM users WHERE username='admin'").fetchone() is None:
                salt = secrets.token_bytes(16)
                db.execute("INSERT INTO users(username,salt,password,role) VALUES('admin',?,?,'admin')",
                           (salt, password_hash(admin_password, salt)))
            db.execute("""UPDATE jobs SET status=CASE status WHEN 'cancelling' THEN 'cancelled' ELSE 'failed' END,
                       stage=CASE status WHEN 'cancelling' THEN 'Скасовано' ELSE 'Роботу перервано' END,
                       error=CASE status WHEN 'cancelling' THEN NULL ELSE 'Сервер перезапущено під час обробки.' END,
                       ended=? WHERE status IN ('running','cancelling')""", (time.time(),))

    @contextmanager
    def connection(self, write=False):
        db = sqlite3.connect(self.path, timeout=8)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                if write:
                    db.execute("BEGIN IMMEDIATE")
                yield db
        finally:
            db.close()

    def register(self, username, password):
        salt = secrets.token_bytes(16)
        digest = password_hash(password, salt)
        with self.connection(write=True) as db:
            if db.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
                raise HTTPError(409, "Цей логін уже використано.")
            if db.execute("SELECT COUNT(*) FROM users").fetchone()[0] >= config.MAX_USERS:
                raise HTTPError(409, "На цьому сервері досягнуто ліміту акаунтів.")
            row = db.execute("INSERT INTO users(username,salt,password,role) VALUES(?,?,?,'user') RETURNING id,username,role",
                             (username, salt, digest)).fetchone()
        return dict(row)

    def authenticate(self, username, password):
        with self.connection() as db:
            row = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        digest = password_hash(password, row["salt"] if row else bytes(16))
        if row is None or not hmac.compare_digest(digest, row["password"]):
            raise HTTPError(401, "Невірний логін або пароль.")
        return {key: row[key] for key in ("id", "username", "role")}

    def create_session(self, user):
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        with self.connection(write=True) as db:
            db.execute("DELETE FROM sessions WHERE expires <= ?", (time.time(),))
            db.execute("""DELETE FROM sessions WHERE owner=? AND token NOT IN
                       (SELECT token FROM sessions WHERE owner=? ORDER BY expires DESC LIMIT 4)""",
                       (user["id"], user["id"]))
            db.execute("INSERT INTO sessions VALUES(?,?,?,?)",
                       (self.token_hash(token), user["id"], csrf, time.time() + config.SESSION_SECONDS))
        return token, {**user, "csrf": csrf}

    @staticmethod
    def token_hash(token):
        return hashlib.sha256(token.encode()).digest()

    def session(self, token):
        if not token:
            return None
        with self.connection() as db:
            row = db.execute("""SELECT u.id,u.username,u.role,s.csrf FROM sessions s JOIN users u ON u.id=s.owner
                             WHERE s.token=? AND s.expires > ?""", (self.token_hash(token), time.time())).fetchone()
        return dict(row) if row else None

    def logout(self, token):
        with self.connection(write=True) as db:
            db.execute("DELETE FROM sessions WHERE token=?", (self.token_hash(token),))

    def submit(self, owner, name, width, height, media, profile, data):
        job_id = uuid.uuid4().hex
        with self.connection(write=True) as db:
            owner_total, owner_active, global_total, global_active = db.execute(
                """SELECT COUNT(CASE WHEN owner=? THEN 1 END),
                   COUNT(CASE WHEN owner=? AND status IN ('queued','running','cancelling') THEN 1 END),
                   COUNT(*), COUNT(CASE WHEN status IN ('queued','running','cancelling') THEN 1 END)
                   FROM jobs""", (owner, owner)).fetchone()
            if owner_active >= config.MAX_ACTIVE_USER or global_active >= config.MAX_ACTIVE_SYSTEM:
                raise HTTPError(429, "Досягнуто ліміту активних задач. Дочекайся завершення або скасуй задачу.")
            if owner_total >= config.MAX_HISTORY_USER or global_total >= config.MAX_HISTORY_SYSTEM:
                raise HTTPError(409, "Історія заповнена. Видали завершені записи або звернись до адміністратора.")
            db.execute("""INSERT INTO jobs(id,owner,name,width,height,media,profile,status,progress,stage,created)
                       VALUES(?,?,?,?,?,?,?,'queued',0,'У черзі',?)""",
                       (job_id, owner, name, width, height, media, profile, time.time()))
            db.execute("INSERT INTO images(id,original) VALUES(?,?)", (job_id, data))
            return dict(db.execute(f"SELECT {FIELDS} FROM jobs WHERE id=?", (job_id,)).fetchone())

    def jobs(self, owner=None, statuses=None):
        query, args = f"SELECT {FIELDS} FROM jobs WHERE 1=1", []
        if owner is not None:
            query += " AND owner=?"
            args.append(owner)
        if statuses:
            query += " AND status IN (" + ",".join("?" for _ in statuses) + ")"
            args.extend(statuses)
        with self.connection() as db:
            return [dict(row) for row in db.execute(query + " ORDER BY created,id", args)]

    def owned(self, db, job_id, owner):
        row = db.execute(f"SELECT {FIELDS} FROM jobs WHERE id=? AND owner=?", (job_id, owner)).fetchone()
        if row is None:
            raise HTTPError(404, "Задачу не знайдено.")
        return dict(row)

    def asset(self, owner, job_id, result):
        column = "result" if result else "original"
        with self.connection() as db:
            job = self.owned(db, job_id, owner)
            data = db.execute(f"SELECT {column} FROM images WHERE id=?", (job_id,)).fetchone()[0]
        if data is None:
            raise HTTPError(404, "Результат ще недоступний.")
        return data, "image/jpeg" if result else job["media"]

    def cancel(self, owner, job_id):
        with self.connection(write=True) as db:
            job = self.owned(db, job_id, owner)
            if job["status"] == "queued":
                db.execute("UPDATE jobs SET status='cancelled',stage='Скасовано',ended=? WHERE id=?", (time.time(), job_id))
            elif job["status"] == "running":
                db.execute("UPDATE jobs SET status='cancelling',stage='Зупинка процесу' WHERE id=?", (job_id,))
            elif job["status"] not in ("cancelling", "cancelled"):
                raise HTTPError(409, "Задачу вже завершено.")
            return self.owned(db, job_id, owner)

    def delete(self, owner, job_id, admin=False):
        with self.connection(write=True) as db:
            if admin:
                entry = db.execute("SELECT owner FROM jobs WHERE id=?", (job_id,)).fetchone()
                owner = entry["owner"] if entry else owner
            job = self.owned(db, job_id, owner)
            if job["status"] not in config.TERMINAL:
                raise HTTPError(409, "Спершу скасуй задачу або дочекайся завершення.")
            db.execute("DELETE FROM jobs WHERE id=?", (job_id,))

    def assign(self, job_id, worker):
        with self.connection(write=True) as db:
            updated = db.execute("""UPDATE jobs SET status='running',stage='Передавання на сервер',worker=?,started=?,progress=1
                                 WHERE id=? AND status='queued'""", (worker, time.time(), job_id)).rowcount
            if updated:
                return db.execute("SELECT original FROM images WHERE id=?", (job_id,)).fetchone()[0]
        return None

    def progress(self, job_id, progress, stage):
        with self.connection() as db:
            db.execute("UPDATE jobs SET progress=MAX(progress,?),stage=? WHERE id=? AND status='running'", (progress, stage, job_id))

    def finish(self, job_id, status, error=None, image=None):
        with self.connection(write=True) as db:
            row = db.execute("SELECT status,progress FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None or row["status"] not in ("running", "cancelling"):
                return
            if row["status"] == "cancelling":
                status, error, image = "cancelled", None, None
            stage = {"completed": "Готово", "cancelled": "Скасовано", "failed": "Помилка"}[status]
            progress = 100 if status == "completed" else row["progress"]
            if image is not None:
                db.execute("UPDATE images SET result=? WHERE id=?", (image, job_id))
            db.execute("UPDATE jobs SET status=?,stage=?,progress=?,error=?,ended=? WHERE id=?",
                       (status, stage, progress, error, time.time(), job_id))
