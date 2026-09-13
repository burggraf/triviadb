"""Run with python3 -m triviadb. No installation or API key needed for imports."""

import argparse
import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import sys
import tempfile
from collections import Counter, defaultdict, deque
from contextlib import contextmanager
from pathlib import Path

from . import media, sources
from .catalog import CATEGORIES, RECIPES, default_fact_eligible
from .gemini import Gemini, Paused, load_config
from .net import CachedHTTP, HTTPFailure
from .pipeline import REVIEW_MODEL, WRITER_MODEL, generate, review_pending
from .store import connect, digest, dumps


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("Must be positive")
    return number


def parser():
    p = argparse.ArgumentParser(prog="triviadb", description="Build original, source-grounded pub trivia in SQLite.")
    p.add_argument("--db", default="data/trivia.sqlite", help="Working SQLite database")
    p.add_argument("--env", default=".env")
    p.add_argument("--cache", default="data/cache", help="Downloaded source snapshots")
    p.add_argument("--media-dir", default="data/media", help="Asset folder; stored media paths are filenames relative to this folder")
    p.add_argument("--max-media-mb", type=positive, default=2048, help="Total media-library cap in MiB (default 2 GiB)")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="Initialize the database")
    sub.add_parser("sources", help="List implemented sources and question recipes")
    sub.add_parser("stats", help="Show saved progress, coverage, and storage")
    keys = sub.add_parser("keys", help="Inspect key counts/cooldowns without exposing secrets")
    keys.add_argument("--reset", action="store_true", help="Explicitly clear saved key cooldown/disabled state")
    sub.add_parser("models", help="List available Gemini models using your keys")
    imp = sub.add_parser("import", help="Download/import source facts; safe to rerun").add_subparsers(dest="source", required=True)
    wd = imp.add_parser("wikidata")
    wd.add_argument("--recipes", default="film-director,book-author,album-artist,element-number,painting-artist", help="Comma-delimited recipe keys, or all")
    wd.add_argument("--pages", type=positive, default=1)
    wd.add_argument("--page-size", type=positive, default=50)
    wd.add_argument("--min-sitelinks", type=positive, default=25)
    wd.add_argument("--ids", help="Optional comma-delimited Wikidata Q IDs, instead of SPARQL discovery")
    met = imp.add_parser("met")
    met.add_argument("--file", type=Path, help="Existing official Met CSV instead of download")
    met.add_argument("--max-records", type=positive, help="Pause after this many source rows; rerun to continue")
    imp.add_parser("natural-earth")
    dump = imp.add_parser("wikidata-dump")
    dump.add_argument("file", type=Path)
    dump.add_argument("--min-sitelinks", type=positive, default=25)
    dump.add_argument("--max-records", type=positive, help="Per-run entity scan budget (two-pass streaming import)")
    for name in ("generate", "review"):
        gen = sub.add_parser(name, help="Write/review candidates" if name == "generate" else "Resume outstanding draft reviews")
        gen.add_argument("--limit", type=positive, default=100, help="Maximum candidates processed, not guaranteed acceptances")
        gen.add_argument("--batch-size", type=positive, default=6)
        gen.add_argument("--max-calls", type=positive, default=100, help="Maximum real API attempts, including retries")
        gen.add_argument("--interval", type=float, default=6, help="Minimum seconds between requests per model across all keys")
        gen.add_argument("--writer-model")
        gen.add_argument("--reviewer-model")
        gen.add_argument("--category", choices=CATEGORIES)
        gen.add_argument("--type", choices=("text", "photo", "sound"))
        gen.add_argument("--include-specialist", action="store_true", help="Opt into narrow specialist fact relationships")
        if name == "generate":
            gen.add_argument("--draft-only", action="store_true", help="Never publish; save drafts for a later review command")
    sample = sub.add_parser("sample", help="Print approved questions as JSON")
    sample.add_argument("--limit", type=positive, default=5)
    sample.add_argument("--category", choices=CATEGORIES)
    sample.add_argument("--type", choices=("text", "photo", "sound"))
    sample.add_argument("--seed", type=int, help="Repeatable shuffled sample (otherwise a new random seed)")
    sample.add_argument("--min-difficulty", type=positive, default=1)
    sample.add_argument("--max-difficulty", type=positive, default=9)
    sample.add_argument("--include-specialist", action="store_true", help="Opt into narrow specialist fact relationships")
    inspect = sub.add_parser("inspect", help="Inspect a fact or question ID, including evidence/review")
    inspect.add_argument("id")
    reject = sub.add_parser("reject", help="Manually unpublish/reject a candidate; retain its fact and audit trail")
    reject.add_argument("id", help="Question UUID or candidate fact ID")
    reject.add_argument("--reason", required=True)
    tune = sub.add_parser("set-difficulty", help="Record a manual editorial difficulty estimate, retaining the original review")
    tune.add_argument("id", help="Approved question UUID")
    tune.add_argument("difficulty", type=positive)
    tune.add_argument("--reason", required=True)
    export = sub.add_parser("export", help="Export approved questions, provenance, and required media")
    export.add_argument("path", type=Path)
    med = sub.add_parser("media", help="Optional image/audio questions").add_subparsers(dest="media_command", required=True)
    med.add_parser("migrate", help="Migrate legacy media to shared question UUIDs and filename-only paths; safe to rerun")
    mm = med.add_parser("met", help="Attach explicitly Open Access Met images to unprocessed facts")
    mm.add_argument("--limit", type=positive, default=10)
    ml = med.add_parser("list")
    ml.add_argument("--limit", type=positive, default=20)
    mi = med.add_parser("import", help="Import an explicitly CC0/public-domain image or recording")
    mi.add_argument("file", type=Path)
    mi.add_argument("--type", choices=("photo", "sound"), required=True)
    mi.add_argument("--license", choices=("CC0-1.0", "public-domain"), required=True)
    for flag in ("source-url", "license-url", "creator", "evidence"):
        mi.add_argument("--" + flag, required=True)
    mi.add_argument("--rights-confirmed", action="store_true", required=True, help="Confirm rights to this specific asset/recording, not just its metadata")
    mi.add_argument("--start", type=float, default=0)
    mi.add_argument("--seconds", type=float, default=10)
    mi.add_argument("--fact-id", help="Optionally attach to this unprocessed fact")
    ma = med.add_parser("attach")
    ma.add_argument("--fact-id", required=True)
    ma.add_argument("--media-id", required=True)
    return p


@contextmanager
def writer_lock(path):
    path = Path(str(path) + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as file:
        try:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Paused("Another writer is using this database. Read-only stats/sample remain available.") from None
        try:
            yield
        finally:
            fcntl.flock(file, fcntl.LOCK_UN)


def print_json(data):
    print(json.dumps(data, ensure_ascii=False, indent=2))


def export_db(db, target, media_dir="data/media"):
    if db.execute("SELECT 1 FROM questions WHERE media_id IS NOT NULL AND media_id<>id").fetchone():
        raise ValueError("Legacy media identity; run `media migrate` before exporting")
    target = Path(target).resolve()
    assets_target = target.parent / (target.stem + ".media")
    if target.exists() or assets_target.exists():
        raise ValueError("Export destination or its media directory already exists; choose a new name")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".trivia-export-", dir=target.parent) as temp:
        temp = Path(temp)
        output = connect(temp / "game.sqlite")
        try:
            selections = {
                "facts": "SELECT f.* FROM facts f JOIN question_meta qm ON qm.fact_id=f.id",
                "media": "SELECT m.* FROM media m WHERE m.id IN (SELECT media_id FROM questions)",
                "questions": "SELECT * FROM questions",
                "question_meta": "SELECT * FROM question_meta",
                "provenance": "SELECT p.* FROM provenance p JOIN question_meta qm ON qm.fact_id=p.fact_id",
                "fact_media": "SELECT fm.* FROM fact_media fm JOIN question_meta qm ON qm.fact_id=fm.fact_id",
            }
            with output:
                for table, sql in selections.items():
                    for row in db.execute(sql):
                        row = dict(row)
                        if table == "media":
                            original = media.media_path(row, media_dir)
                            with original.open("rb") as file:
                                if hashlib.file_digest(file, "sha256").hexdigest() != row["sha256"]:
                                    raise ValueError("Required export media is missing or has changed")
                            relative = Path(assets_target.name) / original.name
                            asset = temp / relative
                            asset.parent.mkdir(exist_ok=True)
                            shutil.copyfile(original, asset)
                            row["path"] = original.name
                        output.execute(f"INSERT INTO {table} VALUES ({','.join('?' for _ in row)})", tuple(row.values()))
            if output.execute("PRAGMA foreign_key_check").fetchall() or output.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Export failed SQLite integrity checks")
            output.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            output.execute("PRAGMA journal_mode=DELETE")
        finally:
            output.close()
        if (temp / assets_target.name).exists():
            (temp / assets_target.name).rename(assets_target)
        os.link(temp / "game.sqlite", target)  # Atomic, refuses to overwrite even if another process won a race.
    print(f"Exported approved questions to {target}")


def set_difficulty(db, identity, difficulty, reason):
    if type(difficulty) is not int or not 1 <= difficulty <= 9 or not reason.strip():
        raise ValueError("A difficulty of 1-9 and an explanation are required")
    row = db.execute("""SELECT c.* FROM question_meta qm JOIN candidates c ON c.fact_id=qm.fact_id
        WHERE qm.question_id=?""", (identity,)).fetchone()
    if not row:
        raise ValueError("Approved question not found in the working database")
    payload = json.loads(row["payload"])
    payload["difficulty"] = difficulty
    with db:
        db.execute("UPDATE questions SET difficulty=? WHERE id=?", (difficulty, identity))
        db.execute("UPDATE candidates SET payload=?,review=? WHERE fact_id=?", (dumps(payload),
                   dumps({"manual_difficulty": difficulty, "reason": reason, "prior_review": json.loads(row["review"])}), row["fact_id"]))
    print(f"Difficulty for {identity}: {difficulty} ({reason})")


def reject_question(db, identity, reason):
    if not reason.strip():
        raise ValueError("Rejection requires a reason")
    row = db.execute("SELECT fact_id FROM question_meta WHERE question_id=?", (identity,)).fetchone()
    fid = row[0] if row else identity
    candidate = db.execute("SELECT * FROM candidates WHERE fact_id=?", (fid,)).fetchone()
    if not candidate:
        raise ValueError("Question/candidate not found")
    with db:
        db.execute("DELETE FROM questions WHERE id IN (SELECT question_id FROM question_meta WHERE fact_id=?)", (fid,))
        db.execute("UPDATE candidates SET status='rejected',reason=?,reviewer_model='manual-editorial',review=? WHERE fact_id=?",
                   (reason, dumps({"manual_rejection": reason, "question_id": identity,
                                   "prior_review": json.loads(candidate["review"]) if candidate["review"] else None}), fid))
    print(f"Rejected candidate {fid}: {reason}")


def sample_rows(db, limit=30, seed=1, category=None, question_type=None, min_difficulty=1, max_difficulty=9,
                include_specialist=False):
    if not 1 <= min_difficulty <= max_difficulty <= 9:
        raise ValueError("Difficulty bounds must satisfy 1 <= min <= max <= 9")
    db.create_function("sample_order", 1, lambda qid: digest([seed, qid]), deterministic=True)
    rows = db.execute("""SELECT q.*, f.pool AS fact_pool, f.subject_id AS fact_subject_id,
            CASE WHEN EXISTS(SELECT 1 FROM provenance p WHERE p.fact_id=f.id AND p.source='met') THEN 'met' ELSE '' END AS fact_source,
            f.popularity AS fact_popularity, EXISTS(SELECT 1 FROM fact_media WHERE fact_id=f.id) AS has_media,
            (q.difficulty-1)/3 AS band
        FROM questions q JOIN question_meta qm ON qm.question_id=q.id JOIN facts f ON f.id=qm.fact_id
        WHERE (? IS NULL OR q.category=?) AND (? IS NULL OR q.type=?)
          AND q.difficulty BETWEEN ? AND ?
        ORDER BY q.category,(q.difficulty-1)/3,sample_order(q.id)""",
                      (category, category, question_type, question_type, min_difficulty, max_difficulty)).fetchall()
    queues, counts, selected = defaultdict(deque), Counter(), []
    eligible_pools = set()
    for row in rows:
        item = dict(row)
        fact = {"pool": item["fact_pool"], "source": item["fact_source"], "popularity": item["fact_popularity"]}
        if not default_fact_eligible(fact, has_media=bool(item["has_media"]), question_type=question_type,
                                     include_specialist=include_specialist):
            continue
        eligible_pools.add(item["fact_pool"])
        band = item.pop("band")
        item.pop("fact_pool")
        item.pop("fact_subject_id")
        item.pop("fact_source")
        item.pop("fact_popularity")
        item.pop("has_media")
        queues[band, item["category"]].append((fact["pool"], row["fact_subject_id"], item))
    pool_cap_limit = (max(2, (limit + len(eligible_pools) - 1) // len(eligible_pools) + 1)
                      if len(eligible_pools) > 1 else limit)
    pool_cap = 2 if len(eligible_pools) > 1 else limit
    used_pools, used_subjects = Counter(), set()
    pattern = [0, 1, 0, 1, 0, 1, 0, 0, 0, 2]  # 60% easy / 30% medium / 10% hard, when available.
    while len(selected) < limit:
        available = [key for key, queue in queues.items() if queue]
        if not available:
            break
        desired = pattern[len(selected) % len(pattern)]
        made = False
        ordered = sorted(available, key=lambda key: (abs(key[0] - desired), counts[key[1]], digest([seed, key[1]])))
        for key in ordered:
            queue = queues[key]
            for _ in range(len(queue)):
                pool, subject_id, item = queue.popleft()
                if used_pools[pool] >= pool_cap or subject_id in used_subjects:
                    queue.append((pool, subject_id, item))
                    continue
                selected.append(item)
                counts[item["category"]] += 1
                used_pools[pool] += 1
                used_subjects.add(subject_id)
                made = True
                break
            if made:
                break
        if not made:
            if pool_cap < pool_cap_limit:
                pool_cap += 1
                continue
            break
    return selected


def stats(db, args):
    result = {"database": str(Path(args.db).resolve())}
    for table in ("facts", "candidates"):
        result[table] = dict(db.execute(f"SELECT status,count(*) FROM {table} GROUP BY status"))
    result["approved"] = db.execute("SELECT count(*) FROM questions").fetchone()[0]
    for column in ("category", "type", "difficulty"):
        result["by_" + column] = dict(db.execute(f"SELECT {column},count(*) FROM questions GROUP BY {column}"))
    result["difficulty_bands"] = dict(db.execute("""SELECT CASE WHEN difficulty<=3 THEN 'easy (1-3)'
        WHEN difficulty<=6 THEN 'medium (4-6)' ELSE 'hard (7-9)' END,count(*) FROM questions GROUP BY 1"""))
    result["difficulty_note"] = "Editorial estimates, not player-calibrated. Balanced samples target 60/30/10 easy/medium/hard when coverage allows."
    result["source_facts"] = dict(db.execute("SELECT source,count(DISTINCT fact_id) FROM provenance GROUP BY source"))
    result["media"] = dict(db.execute("SELECT count(*) AS files,coalesce(sum(bytes),0) AS bytes FROM media").fetchone())
    result["source_cache_bytes"] = sum(path.stat().st_size for path in Path(args.cache).rglob("*") if path.is_file())
    result["disk_free_bytes"] = shutil.disk_usage(Path(args.db).resolve().parent).free
    result["completed_model_jobs"] = db.execute("SELECT count(*) FROM jobs").fetchone()[0]
    result["rejection_reasons"] = [dict(r) for r in db.execute("SELECT reason,count(*) AS count FROM candidates WHERE status='rejected' GROUP BY reason ORDER BY count(*) DESC LIMIT 10")]
    print_json(result)


def execute(db, args):
    http = CachedHTTP(db, args.cache)
    if args.command == "init":
        print(f"Initialized {Path(args.db).resolve()}")
    elif args.command == "sources":
        print_json({"implemented": {"wikidata": "CC0-1.0; API + local JSON dumps", "met": "CC0-1.0; CSV highlights + Open Access photos",
                                   "natural-earth": "public-domain; downloaded city coordinates -> calculated questions"},
                    "recipes": {key: {"category": row[2], "subcategory": row[3], "relationship": row[4]} for key, row in RECIPES.items()}})
    elif args.command == "import":
        if args.source == "wikidata":
            recipes = list(RECIPES) if args.recipes == "all" else [r.strip() for r in args.recipes.split(",")]
            ids = [x.strip() for x in args.ids.split(",")] if args.ids else None
            if args.page_size > 50 or (ids and any(not re.fullmatch(r"Q[1-9]\d*", x) for x in ids)):
                raise ValueError("Page size must be <=50 and IDs must be Wikidata Q IDs")
            sources.import_wikidata(db, http, recipes, args.pages, args.page_size, args.min_sitelinks, ids)
        elif args.source == "met":
            sources.import_met(db, http, args.file, args.max_records)
        elif args.source == "natural-earth":
            sources.import_natural(db, http)
        else:
            sources.import_dump(db, args.file, args.min_sitelinks, args.max_records)
    elif args.command in ("generate", "review", "models", "keys"):
        keys, projects, config = load_config(args.env)
        if args.command == "keys":
            if args.reset:
                with db:
                    db.execute("DELETE FROM key_state")
            print_json({"configured_unique_keys": len(keys), "quota_note": "Quotas are per project, not per key.",
                        "state": [dict(r) for r in db.execute("SELECT substr(fingerprint,1,10) AS key_id,model,next_at,disabled,failures FROM key_state ORDER BY model,key_id")]})
            return
        if args.command == "models":
            client = Gemini(db, keys, projects, max_calls=21)
            result = client.call("models", "catalog")
            print_json([m["name"].removeprefix("models/") for m in result.get("models", []) if "generateContent" in m.get("supportedGenerationMethods", [])])
            return
        if args.batch_size > 20 or not 0 <= args.interval <= 3600:
            raise ValueError("Batch size must be <=20; interval must be 0-3600 seconds")
        client = Gemini(db, keys, projects, interval=args.interval, max_calls=args.max_calls)
        writer = args.writer_model or config.get("GEMINI_WRITER_MODEL", WRITER_MODEL)
        reviewer = args.reviewer_model or config.get("GEMINI_REVIEWER_MODEL", REVIEW_MODEL)
        if args.command == "generate":
            generate(db, client, args.limit, args.batch_size, writer, reviewer, args.draft_only, args.category, args.type,
                     args.media_dir, args.include_specialist)
        else:
            review_pending(db, client, args.limit, args.batch_size, reviewer, args.category, args.type, args.media_dir,
                           args.include_specialist)
        print(f"API attempts this run: {client.calls}; approved total: {db.execute('SELECT count(*) FROM questions').fetchone()[0]}")
    elif args.command == "stats":
        stats(db, args)
    elif args.command == "sample":
        seed = args.seed if args.seed is not None else secrets.randbits(32)
        print(f"Sample seed: {seed}; category-balanced, varied by source relationship, target easy/medium/hard 60/30/10 where available.", file=sys.stderr)
        print_json(sample_rows(db, args.limit, seed, args.category, args.type, args.min_difficulty, args.max_difficulty,
                               args.include_specialist))
    elif args.command == "inspect":
        row = db.execute("SELECT fact_id FROM question_meta WHERE question_id=?", (args.id,)).fetchone()
        fid = row[0] if row else args.id
        result = {}
        for table, column in (("facts", "id"), ("candidates", "fact_id"), ("provenance", "fact_id"), ("fact_media", "fact_id")):
            result[table] = [dict(r) for r in db.execute(f"SELECT * FROM {table} WHERE {column}=?", (fid,))]
        print_json(result)
    elif args.command == "set-difficulty":
        set_difficulty(db, args.id, args.difficulty, args.reason)
    elif args.command == "reject":
        reject_question(db, args.id, args.reason)
    elif args.command == "export":
        export_db(db, args.path, args.media_dir)
    elif args.command == "media":
        cap = args.max_media_mb * 1024**2
        if args.media_command == "migrate":
            media.migrate(db, args.media_dir)
        elif args.media_command == "met":
            media.import_met_media(db, http, args.media_dir, args.limit, cap)
        elif args.media_command == "list":
            print_json([dict(r) for r in db.execute("SELECT * FROM media ORDER BY created_at DESC LIMIT ?", (args.limit,))])
        elif args.media_command == "attach":
            media.attach(db, args.fact_id, args.media_id)
        else:
            mid = media.import_asset(db, args.file, args.media_dir, kind=args.type, license_id=args.license,
                                     rights_confirmed=args.rights_confirmed, source_url=args.source_url, license_url=args.license_url,
                                     creator=args.creator, evidence=args.evidence, start=args.start, seconds=args.seconds, max_bytes=cap)
            if args.fact_id:
                media.attach(db, args.fact_id, mid)
            print(f"Imported {args.type}: {mid}")


def main(argv=None):
    args = parser().parse_args(argv)
    db = None
    try:
        db = connect(args.db)
        if args.command in ("stats", "sample", "inspect", "sources") or (args.command == "media" and args.media_command == "list"):
            execute(db, args)
        else:
            with writer_lock(args.db):
                execute(db, args)
        return 0
    except KeyboardInterrupt:
        print("\nInterrupted. Committed progress is saved; rerun the same command to resume.", file=sys.stderr)
        return 130
    except Paused as exc:
        print(f"Paused: {exc}", file=sys.stderr)
        return 75
    except (ValueError, RuntimeError, OSError, sqlite3.Error) as exc:
        print(f"Error: {exc}. Committed progress is retained.", file=sys.stderr)
        return 1
    finally:
        if db:
            db.close()


if __name__ == "__main__":
    sys.exit(main())
