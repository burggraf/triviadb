"""SQLite owns identity, provenance, queues, and the approved game database."""

import hashlib
import html
import json
import re
import sqlite3
import unicodedata
import uuid
from pathlib import Path

from .catalog import CATEGORIES

FIELDS = ("category", "subcategory", "question", "a", "b", "c", "d", "difficulty", "notes", "type", "media_id")
REVIEW_CHECKS = (
    "supported", "one_correct", "clear", "timeless", "plausible_distractors",
    "no_answer_leak", "notes_supported", "us_adult_appropriate", "pub_worthy", "media_matches",
)


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(dumps(value).encode()).hexdigest()


def normalize(text):
    return " ".join(re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", html.unescape(text)).casefold()))


def entity_id(value):
    match = re.fullmatch(r"(?:wd:|https?://www\.wikidata\.org/(?:wiki|entity)/)?(Q[1-9]\d*)", value)
    return "wd:" + match[1] if match else value


def canonical(value):
    if isinstance(value, dict):
        return {key: canonical(val) for key, val in sorted(value.items())}
    if isinstance(value, list):
        return sorted((canonical(val) for val in value), key=dumps)
    return value


def connect(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA journal_mode=WAL")
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version not in (0, 1):
        db.close()
        raise ValueError(f"Unsupported database version {version}; upgrade TriviaDB before opening it.")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS categories (name TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS facts (
            id TEXT PRIMARY KEY, slot TEXT NOT NULL, subject_id TEXT NOT NULL,
            subject TEXT NOT NULL, predicate TEXT NOT NULL, object_id TEXT NOT NULL,
            answer TEXT NOT NULL, category TEXT NOT NULL REFERENCES categories(name),
            subcategory TEXT NOT NULL, pool TEXT NOT NULL, popularity INTEGER NOT NULL,
            qualifiers TEXT NOT NULL, context TEXT NOT NULL, fixed_options TEXT,
            status TEXT NOT NULL DEFAULT 'ready' CHECK(status IN ('ready','conflict'))
        );
        CREATE INDEX IF NOT EXISTS facts_slot ON facts(slot);
        CREATE INDEX IF NOT EXISTS facts_pool ON facts(pool, status, popularity DESC);
        CREATE INDEX IF NOT EXISTS facts_category ON facts(category, status, popularity DESC);
        CREATE TABLE IF NOT EXISTS provenance (
            fact_id TEXT NOT NULL REFERENCES facts(id), source TEXT NOT NULL,
            source_record_id TEXT NOT NULL, source_url TEXT NOT NULL,
            license TEXT NOT NULL CHECK(license IN ('CC0-1.0','public-domain')),
            snapshot TEXT NOT NULL, retrieved_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(fact_id, source, source_record_id, snapshot)
        );
        CREATE TABLE IF NOT EXISTS candidates (
            fact_id TEXT PRIMARY KEY REFERENCES facts(id), payload TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('draft','accepted','rejected','conflict')),
            reason TEXT NOT NULL DEFAULT '', writer_model TEXT NOT NULL,
            reviewer_model TEXT, review TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS candidates_status ON candidates(status);
        CREATE TABLE IF NOT EXISTS media (
            id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('photo','sound')),
            path TEXT NOT NULL, sha256 TEXT NOT NULL UNIQUE, bytes INTEGER NOT NULL,
            mime_type TEXT NOT NULL, source_url TEXT NOT NULL, creator TEXT NOT NULL,
            license TEXT NOT NULL CHECK(license IN ('CC0-1.0','public-domain')),
            license_url TEXT NOT NULL, evidence TEXT NOT NULL, attribution TEXT NOT NULL,
            alt_text TEXT NOT NULL, start_seconds REAL, duration_seconds REAL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS fact_media (
            fact_id TEXT PRIMARY KEY REFERENCES facts(id), media_id TEXT NOT NULL REFERENCES media(id)
        );
        CREATE TABLE IF NOT EXISTS questions (
            id TEXT PRIMARY KEY CHECK(length(id)=36 AND substr(id,15,1)='4'
                AND substr(id,20,1) IN ('8','9','a','b')),
            category TEXT NOT NULL REFERENCES categories(name),
            subcategory TEXT NOT NULL CHECK(length(trim(subcategory))>0),
            question TEXT NOT NULL CHECK(length(trim(question))>0),
            a TEXT NOT NULL CHECK(length(trim(a))>0), b TEXT NOT NULL CHECK(length(trim(b))>0),
            c TEXT NOT NULL CHECK(length(trim(c))>0), d TEXT NOT NULL CHECK(length(trim(d))>0),
            difficulty INTEGER NOT NULL CHECK(typeof(difficulty)='integer' AND difficulty BETWEEN 1 AND 9),
            notes TEXT,
            type TEXT NOT NULL DEFAULT 'text' CHECK(type IN ('text','photo','sound')),
            media_id TEXT REFERENCES media(id),
            CHECK((type='text' AND media_id IS NULL) OR (type<>'text' AND media_id IS NOT NULL)),
            CHECK(a<>b AND a<>c AND a<>d AND b<>c AND b<>d AND c<>d)
        );
        CREATE TABLE IF NOT EXISTS question_meta (
            question_id TEXT PRIMARY KEY REFERENCES questions(id) ON DELETE CASCADE,
            fact_id TEXT NOT NULL UNIQUE REFERENCES facts(id), question_norm TEXT NOT NULL UNIQUE,
            approved_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS checkpoints (name TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, stage TEXT NOT NULL, model TEXT NOT NULL,
            response TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS key_state (
            fingerprint TEXT NOT NULL, model TEXT NOT NULL, project TEXT NOT NULL,
            next_at REAL NOT NULL DEFAULT 0, last_used REAL NOT NULL DEFAULT 0,
            disabled INTEGER NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(fingerprint, model)
        );
        PRAGMA user_version=1;
    """)
    with db:
        db.executemany("INSERT OR IGNORE INTO categories VALUES (?)", ((cat,) for cat in CATEGORIES))
    return db


def checkpoint(db, name, value=None):
    if value is not None:
        db.execute("INSERT INTO checkpoints VALUES (?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                   (name, dumps(value)))
    row = db.execute("SELECT value FROM checkpoints WHERE name=?", (name,)).fetchone()
    return json.loads(row[0]) if row else {}


def add_fact(db, data):
    data = dict(data)
    for key in ("subject_id", "subject", "predicate", "object_id", "answer", "subcategory", "pool",
                "source", "source_record_id", "source_url", "snapshot"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise ValueError(f"Fact requires nonempty {key}")
    if data.get("license") not in ("CC0-1.0", "public-domain") or data.get("category") not in CATEGORIES:
        raise ValueError("Only defined categories and CC0/public-domain facts are allowed")
    if not re.match(r"https?://[^/]+", data["source_url"]):
        raise ValueError("Fact provenance requires an HTTP(S) source URL")
    data["subject_id"], data["object_id"] = entity_id(data["subject_id"]), entity_id(data["object_id"])
    qualifiers = canonical(data.get("qualifiers", {}))
    scope = [data["subject_id"], data["predicate"], qualifiers]
    fid, slot = digest([*scope, data["object_id"]]), digest(scope)
    conflict = db.execute("SELECT 1 FROM facts WHERE slot=? AND (id<>? OR status='conflict')", (slot, fid)).fetchone()
    keys = ("subject_id", "subject", "predicate", "object_id", "answer", "category", "subcategory", "pool")
    db.execute("""INSERT OR IGNORE INTO facts
        (id,slot,subject_id,subject,predicate,object_id,answer,category,subcategory,pool,
         popularity,qualifiers,context,fixed_options,status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
               (fid, slot, *(data[key] for key in keys), int(data.get("popularity", 0)), dumps(qualifiers),
                dumps(data.get("context", {})), dumps(data["fixed_options"]) if data.get("fixed_options") else None,
                "conflict" if conflict else "ready"))
    db.execute("""INSERT OR IGNORE INTO provenance
        (fact_id,source,source_record_id,source_url,license,snapshot) VALUES (?,?,?,?,?,?)""",
               (fid, *(data[key] for key in ("source", "source_record_id", "source_url", "license", "snapshot"))))
    if conflict:
        db.execute("UPDATE facts SET status='conflict' WHERE slot=?", (slot,))
        db.execute("""DELETE FROM questions WHERE id IN (SELECT question_id FROM question_meta
            JOIN facts ON facts.id=question_meta.fact_id WHERE facts.slot=?)""", (slot,))
        db.execute("""UPDATE candidates SET status='conflict',reason='Source answer conflict'
            WHERE fact_id IN (SELECT id FROM facts WHERE slot=?)""", (slot,))
    return fid


def unpack(row):
    result = dict(row)
    for key in ("qualifiers", "context", "fixed_options"):
        if key in result and isinstance(result[key], str):
            result[key] = json.loads(result[key])
    return result


def validate_question(q, fact):
    if not isinstance(q, dict) or set(q) != set(FIELDS):
        raise ValueError("Question must contain exactly the game fields")
    if q["type"] not in ("text", "photo", "sound") or (q["type"] == "text") != (q["media_id"] is None):
        raise ValueError("Photo/sound questions require media; text questions must not require it")
    if q["media_id"] is not None and not isinstance(q["media_id"], str):
        raise ValueError("Invalid media ID")
    for key in ("category", "subcategory", "question", "a", "b", "c", "d"):
        if not isinstance(q[key], str) or not q[key].strip() or len(q[key]) > (450 if key == "question" else 160):
            raise ValueError(f"Invalid question field: {key}")
    if q["category"] not in CATEGORIES or q["category"] != fact["category"] or q["subcategory"] != fact["subcategory"]:
        raise ValueError("Question taxonomy must match its fact")
    if q["a"] != fact["answer"]:
        raise ValueError("The correct answer must be copied from the fact")
    choices = [normalize(q[key]) for key in "abcd"]
    if any(not value for value in choices) or len(set(choices)) != 4:
        raise ValueError("Four distinct nonempty answer choices are required")
    if type(q["difficulty"]) is not int or not 1 <= q["difficulty"] <= 9:
        raise ValueError("Difficulty must be an integer from 1 to 9")
    if q["notes"] is not None and (not isinstance(q["notes"], str) or len(q["notes"]) > 650):
        raise ValueError("Notes must be text of at most 650 characters or null")
    if re.search(r"\b(current|currently|nowadays|today|latest|this year|last year|next year|recently)\b", q["question"], re.I):
        raise ValueError("Unanchored relative-time wording")
    if f" {choices[0]} " in f" {normalize(q['question'])} ":
        raise ValueError("Question reveals its correct answer")
    if q["type"] != "text" and f" {normalize(fact['subject'])} " in f" {normalize(q['question'])} ":
        raise ValueError("Media question supplies the work title, making its attachment unnecessary")
    if any(value in {"all of the above", "none of the above", "both a and b"} for value in choices):
        raise ValueError("Meta answer choices are not allowed")


def save_candidate(db, fid, q, model):
    fact = db.execute("SELECT * FROM facts WHERE id=?", (fid,)).fetchone()
    if not fact or fact["status"] != "ready":
        raise ValueError("Fact is missing or quarantined")
    reason = ""
    try:
        validate_question(q, fact)
    except ValueError as exc:
        reason = str(exc)
    db.execute("""INSERT OR IGNORE INTO candidates(fact_id,payload,status,reason,writer_model)
        VALUES (?,?,?,?,?)""", (fid, dumps(q), "rejected" if reason else "draft", reason, model))


def apply_review(db, fid, review, model):
    row = db.execute("SELECT * FROM candidates WHERE fact_id=?", (fid,)).fetchone()
    if not row or row["status"] != "draft":
        return
    if not isinstance(review, dict) or review.get("verdict") not in ("accept", "reject"):
        raise ValueError("Malformed review verdict; draft remains pending")
    checks = review.get("checks")
    if not isinstance(checks, dict) or set(checks) != set(REVIEW_CHECKS) or any(type(v) is not bool for v in checks.values()):
        raise ValueError("Malformed review checks; draft remains pending")
    if type(review.get("difficulty")) is not int or not 1 <= review["difficulty"] <= 9:
        raise ValueError("Invalid reviewed difficulty")
    if not isinstance(review.get("reason"), str) or not review["reason"].strip():
        raise ValueError("Review must explain its verdict")
    fact = db.execute("SELECT * FROM facts WHERE id=?", (fid,)).fetchone()
    q = json.loads(row["payload"])
    q["difficulty"] = review["difficulty"]
    validate_question(q, fact)
    accept = review["verdict"] == "accept" and all(checks.values()) and fact["status"] == "ready"
    reason = review["reason"]
    norm = normalize(q["question"])
    # The stimulus is part of a media/comparison question's identity, not just its generic stem.
    if q["media_id"]:
        norm += " | media:" + q["media_id"]
    elif fact["fixed_options"]:
        norm += " | choices:" + dumps(sorted(normalize(q[key]) for key in "abcd"))
    if accept and db.execute("SELECT 1 FROM question_meta WHERE question_norm=?", (norm,)).fetchone():
        accept, reason = False, "Duplicate normalized question text"
    if accept and q["media_id"]:
        media = db.execute("""SELECT media.* FROM media JOIN fact_media ON media.id=fact_media.media_id
            WHERE fact_media.fact_id=? AND media.id=?""", (fid, q["media_id"])).fetchone()
        if not media or media["kind"] != q["type"] or not Path(media["path"]).is_file():
            raise ValueError("Required media is missing or mismatched; draft remains pending")
        with Path(media["path"]).open("rb") as file:
            if hashlib.file_digest(file, "sha256").hexdigest() != media["sha256"]:
                raise ValueError("Required media checksum mismatch; draft remains pending")
    if accept:
        qid = str(uuid.uuid4())
        db.execute("INSERT INTO questions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (qid, *(q[key] for key in FIELDS)))
        db.execute("INSERT INTO question_meta(question_id,fact_id,question_norm) VALUES (?,?,?)", (qid, fid, norm))
    db.execute("UPDATE candidates SET status=?,reason=?,reviewer_model=?,review=? WHERE fact_id=?",
               ("accepted" if accept else "rejected", reason, model, dumps(review), fid))
