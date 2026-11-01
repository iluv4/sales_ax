"""Canonical database. Legacy databases are never opened for writing."""
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS campaigns(id INTEGER PRIMARY KEY, name TEXT NOT NULL, motion TEXT NOT NULL,
 sender TEXT NOT NULL, icp TEXT NOT NULL, product TEXT NOT NULL DEFAULT '', value_prop TEXT NOT NULL DEFAULT '',
 booking_link TEXT NOT NULL DEFAULT '', target_daily INTEGER NOT NULL DEFAULT 200, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS organizations(id INTEGER PRIMARY KEY, name TEXT NOT NULL, domain TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS contacts(id INTEGER PRIMARY KEY, org_id INTEGER REFERENCES organizations(id),
 name TEXT NOT NULL, title TEXT NOT NULL DEFAULT '', email TEXT UNIQUE, source_url TEXT NOT NULL DEFAULT '',
 verified INTEGER NOT NULL DEFAULT 0, suppressed INTEGER NOT NULL DEFAULT 0, suppression_reason TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS leads(id INTEGER PRIMARY KEY, campaign_id INTEGER NOT NULL REFERENCES campaigns(id),
 contact_id INTEGER NOT NULL REFERENCES contacts(id), stage TEXT NOT NULL DEFAULT 'DISCOVERED', score INTEGER,
 fit TEXT NOT NULL DEFAULT '', rationale TEXT NOT NULL DEFAULT '', research TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL, UNIQUE(campaign_id,contact_id), CHECK(score IS NULL OR score BETWEEN 0 AND 100));
CREATE TABLE IF NOT EXISTS evidence(id INTEGER PRIMARY KEY, contact_id INTEGER NOT NULL REFERENCES contacts(id),
 claim TEXT NOT NULL, source_url TEXT NOT NULL, observed_at TEXT NOT NULL, human_checked INTEGER NOT NULL DEFAULT 0,
 confidence REAL NOT NULL DEFAULT 0.5, UNIQUE(contact_id,claim,source_url));
CREATE TABLE IF NOT EXISTS proposals(id INTEGER PRIMARY KEY, lead_id INTEGER NOT NULL REFERENCES leads(id),
 filename TEXT NOT NULL, content BLOB NOT NULL, sha256 TEXT NOT NULL, recipient_email TEXT NOT NULL,
 recipient_name TEXT NOT NULL, identity_checked INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS drafts(id INTEGER PRIMARY KEY, lead_id INTEGER NOT NULL REFERENCES leads(id),
 step INTEGER NOT NULL DEFAULT 0, subject TEXT NOT NULL, body TEXT NOT NULL, proposal_id INTEGER REFERENCES proposals(id),
 status TEXT NOT NULL DEFAULT 'DRAFT_CREATED', qa_errors TEXT NOT NULL DEFAULT '[]', version INTEGER NOT NULL DEFAULT 1,
 approved_version INTEGER, approved_at TEXT, approved_by TEXT, gmail_draft_id TEXT, gmail_hash TEXT,
 sync_state TEXT NOT NULL DEFAULT 'LOCAL', in_reply_to TEXT NOT NULL DEFAULT '', thread_id TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL, UNIQUE(lead_id,step));
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY, account TEXT NOT NULL, gmail_id TEXT NOT NULL,
 thread_id TEXT NOT NULL, rfc_id TEXT NOT NULL DEFAULT '', lead_id INTEGER NOT NULL REFERENCES leads(id),
 direction TEXT NOT NULL, intent TEXT NOT NULL DEFAULT '', subject TEXT NOT NULL DEFAULT '', snippet TEXT NOT NULL DEFAULT '',
 occurred_at TEXT NOT NULL, UNIQUE(account,gmail_id));
CREATE TABLE IF NOT EXISTS meetings(id INTEGER PRIMARY KEY, lead_id INTEGER NOT NULL REFERENCES leads(id),
 external_id TEXT UNIQUE, starts_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'BOOKED',
 completed_at TEXT, confirmed_by TEXT, notes TEXT NOT NULL DEFAULT '', next_step TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS deals(id INTEGER PRIMARY KEY, lead_id INTEGER NOT NULL UNIQUE REFERENCES leads(id),
 stage TEXT NOT NULL DEFAULT 'DISCOVERY', amount_krw INTEGER NOT NULL DEFAULT 0, evidence TEXT NOT NULL,
 confirmed_by TEXT NOT NULL, verified_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tasks(id INTEGER PRIMARY KEY, lead_id INTEGER NOT NULL REFERENCES leads(id),
 kind TEXT NOT NULL, title TEXT NOT NULL, due_date TEXT NOT NULL, step INTEGER NOT NULL DEFAULT 0,
 status TEXT NOT NULL DEFAULT 'open', UNIQUE(lead_id,kind,step));
CREATE TABLE IF NOT EXISTS templates(id INTEGER PRIMARY KEY, name TEXT NOT NULL, filename TEXT NOT NULL,
 content BLOB NOT NULL, sha256 TEXT NOT NULL, mappings TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit_logs(id INTEGER PRIMARY KEY, lead_id INTEGER REFERENCES leads(id),
 action TEXT NOT NULL, detail TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sync_runs(id INTEGER PRIMARY KEY, source TEXT NOT NULL, status TEXT NOT NULL,
 detail TEXT NOT NULL, at TEXT NOT NULL);
"""


class DB:
    def __init__(self, path):
        if str(path) != ':memory:':
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.conn.executescript(SCHEMA)
        from .migrations import migrate
        migrate(self.conn)

    @contextmanager
    def transaction(self):
        with self.lock:
            try:
                yield
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    def rows(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        rows = self.rows(sql, args)
        return rows[0] if rows else None

    def execute(self, sql, args=()):
        with self.lock:
            return self.conn.execute(sql, args)

    def log(self, action, detail, lead_id=None):
        self.execute('INSERT INTO audit_logs(lead_id,action,detail,at) VALUES(?,?,?,?)',
                     (lead_id, action, json.dumps(detail, ensure_ascii=False), now()))
