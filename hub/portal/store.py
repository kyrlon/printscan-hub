"""Small SQLite store for portal accounts and scan-file ownership.

SQLite (WAL mode) is safe to share between the gunicorn worker processes.
A file with no row in `files` is shared: everyone sees it. That covers old
scans, scans made in shared mode, and scans the printer sends to the inbox.
"""
import os
import sqlite3
import time
from contextlib import contextmanager

from werkzeug.security import check_password_hash, generate_password_hash

DB_PATH = os.environ.get("PORTAL_DB", "/data/portal/portal.db")


@contextmanager
def db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init():
    with db() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("""CREATE TABLE IF NOT EXISTS users (
                       username TEXT PRIMARY KEY,
                       pw_hash  TEXT NOT NULL,
                       created  REAL NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS files (
                       name    TEXT PRIMARY KEY,
                       owner   TEXT NOT NULL,      -- 'b:<browser id>' or 'u:<username>'
                       created REAL NOT NULL)""")
        c.execute("CREATE INDEX IF NOT EXISTS files_owner ON files(owner)")


# ---------------------------------------------------------------- users ---
def user_count():
    with db() as c:
        return c.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def list_users():
    with db() as c:
        rows = c.execute("SELECT username, created FROM users ORDER BY username").fetchall()
        return [{"username": r["username"],
                 "created_str": time.strftime("%b %d, %Y", time.localtime(r["created"])),
                 "files": c.execute("SELECT COUNT(*) FROM files WHERE owner=?",
                                    ("u:" + r["username"],)).fetchone()[0]}
                for r in rows]


def create_user(username, password):
    with db() as c:
        c.execute("INSERT INTO users(username, pw_hash, created) VALUES (?,?,?)",
                  (username, generate_password_hash(password), time.time()))


def set_password(username, password):
    with db() as c:
        return c.execute("UPDATE users SET pw_hash=? WHERE username=?",
                         (generate_password_hash(password), username)).rowcount == 1


def delete_user(username):
    """Delete the account. Returns the names of that user's files (the caller deletes them)."""
    with db() as c:
        names = [r[0] for r in c.execute("SELECT name FROM files WHERE owner=?", ("u:" + username,))]
        c.execute("DELETE FROM users WHERE username=?", (username,))
        return names


def check_login(username, password):
    with db() as c:
        row = c.execute("SELECT pw_hash FROM users WHERE username=?", (username,)).fetchone()
    if row is None:
        check_password_hash(generate_password_hash("x"), password)  # similar timing either way
        return False
    return check_password_hash(row["pw_hash"], password)


# ---------------------------------------------------------------- files ---
def owners():
    with db() as c:
        return {r["name"]: r["owner"] for r in c.execute("SELECT name, owner FROM files")}


def owner_of(name):
    with db() as c:
        row = c.execute("SELECT owner FROM files WHERE name=?", (name,)).fetchone()
    return row["owner"] if row else None


def claim(name, owner):
    with db() as c:
        c.execute("INSERT OR REPLACE INTO files(name, owner, created) VALUES (?,?,?)",
                  (name, owner, time.time()))


def files_of(owner):
    """Owner's files, oldest first."""
    with db() as c:
        return [dict(r) for r in c.execute(
            "SELECT name, created FROM files WHERE owner=? ORDER BY created", (owner,))]


def rename(old, new):
    with db() as c:
        c.execute("UPDATE files SET name=? WHERE name=?", (new, old))


def forget(names):
    with db() as c:
        c.executemany("DELETE FROM files WHERE name=?", [(n,) for n in names])


def expired(prefix, max_age_seconds):
    """Files of owners starting with `prefix` older than max_age_seconds."""
    with db() as c:
        return [r[0] for r in c.execute(
            "SELECT name FROM files WHERE owner LIKE ? AND created < ?",
            (prefix + "%", time.time() - max_age_seconds))]
