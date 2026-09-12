import hashlib
import json
import shutil
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

from triviadb import media, store
from test_triviadb import DatabaseTest, approval, fact, question


class MediaPathTests(DatabaseTest):
    def seed(self, *, legacy=True, published=True):
        self.library = Path(self.temp.name) / "assets"
        self.library.mkdir(exist_ok=True)
        mid = str(uuid.uuid4())
        raw = ("Media fixture " + mid).encode()
        sha = hashlib.sha256(raw).hexdigest()
        path = self.library / ((sha if legacy else mid) + ".jpg")
        path.write_bytes(raw)
        with self.db:
            self.db.execute("""INSERT INTO media
                (id,kind,path,sha256,bytes,mime_type,source_url,creator,license,license_url,evidence,attribution,alt_text)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (mid, "photo", str(path) if legacy else path.name, sha, len(raw), "image/jpeg",
                 "https://example.org/fixture", "Test", "CC0-1.0", "https://example.org/license",
                 "Synthetic test fixture", "Test", "Test image"))
            number = self.db.execute("SELECT count(*) FROM facts").fetchone()[0] + 1
            fid = store.add_fact(self.db, fact(subject=f"wd:Q{number}"))
        media.attach(self.db, fid, mid)
        q = dict(question(), question="Which filmmaker is pictured?", type="photo", media_id=mid)
        with self.db:
            store.save_candidate(self.db, fid, q, "writer")
            if published:
                # Deliberately seed the previous release's two independent UUIDs.
                qid = str(uuid.uuid4())
                self.db.execute("INSERT INTO questions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (qid, *(q[k] for k in store.FIELDS)))
                self.db.execute("INSERT INTO question_meta(question_id,fact_id,question_norm) VALUES (?,?,?)",
                                (qid, fid, store.normalize(q["question"]) + " | media:" + mid))
                self.db.execute("UPDATE candidates SET status='accepted',review=? WHERE fact_id=?", (store.dumps(approval()), fid))
            else:
                qid = mid
        return fid, mid, qid, path, sha

    def test_migration_preserves_question_ids_and_cleans_aliases_on_rerun(self):
        fid, old_id, qid, old, sha = self.seed()
        # Legacy exports stored a relative folder prefix instead of an absolute path.
        with self.db:
            self.db.execute("UPDATE media SET path=?", ("party.media/" + old.name,))
        media.migrate(self.db, self.library)
        row = self.db.execute("SELECT * FROM media").fetchone()
        self.assertEqual((row["id"], row["path"], row["sha256"]), (qid, qid + ".jpg", sha))
        self.assertEqual(tuple(self.db.execute("SELECT id,media_id FROM questions").fetchone()), (qid, qid))
        self.assertEqual(self.db.execute("SELECT media_id FROM fact_media").fetchone()[0], qid)
        self.assertEqual(json.loads(self.db.execute("SELECT payload FROM candidates").fetchone()[0])["media_id"], qid)
        self.assertTrue(self.db.execute("SELECT question_norm FROM question_meta").fetchone()[0].endswith(qid))
        self.assertFalse(old.exists())
        self.assertEqual(hashlib.sha256((self.library / row["path"]).read_bytes()).hexdigest(), sha)
        # Simulate a crash after the DB commit but before deleting old hash aliases.
        shutil.copyfile(self.library / row["path"], old)
        media.migrate(self.db, self.library)
        media.migrate(self.db, self.library)
        self.assertFalse(old.exists())
        self.assertEqual(self.db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_migration_preflights_all_files_before_mutating_any_row(self):
        _, mid, qid, old, _ = self.seed()
        _, _, _, bad, _ = self.seed()
        bad.write_bytes(b"changed")
        with self.assertRaises(ValueError):
            media.migrate(self.db, self.library)
        self.assertIsNotNone(self.db.execute("SELECT 1 FROM media WHERE id=?", (mid,)).fetchone())
        self.assertTrue(old.exists())
        self.assertFalse((self.library / (qid + ".jpg")).exists())

    def test_migration_never_overwrites_a_destination_collision(self):
        _, mid, qid, old, _ = self.seed()
        target = self.library / (qid + ".jpg")
        target.write_bytes(b"not ours")
        with self.assertRaises(ValueError):
            media.migrate(self.db, self.library)
        self.assertEqual(target.read_bytes(), b"not ours")
        self.assertTrue(old.exists())
        self.assertIsNotNone(self.db.execute("SELECT 1 FROM media WHERE id=?", (mid,)).fetchone())

    def test_migration_resumes_after_sqlite_failure_without_losing_originals(self):
        _, mid, qid, old, _ = self.seed()
        self.db.execute("""CREATE TRIGGER stop_migration BEFORE UPDATE ON media
            BEGIN SELECT RAISE(ABORT, 'simulated failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            media.migrate(self.db, self.library)
        self.assertTrue(old.exists())
        self.assertIsNotNone(self.db.execute("SELECT 1 FROM media WHERE id=?", (mid,)).fetchone())
        self.assertEqual(self.db.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.db.execute("DROP TRIGGER stop_migration")
        media.migrate(self.db, self.library)
        self.assertTrue((self.library / (qid + ".jpg")).is_file())
        self.assertFalse(old.exists())

    def test_shared_assets_are_rejected_in_attachment_and_legacy_migration(self):
        _, mid, _, old, _ = self.seed(published=False)
        with self.db:
            second = store.add_fact(self.db, fact(subject="wd:Q2"))
        with self.assertRaises(ValueError):
            media.attach(self.db, second, mid)
        # Previous versions allowed this; migration must refuse to merge questions.
        with self.db:
            self.db.execute("INSERT INTO fact_media VALUES (?,?)", (second, mid))
        with self.assertRaises(ValueError):
            media.migrate(self.db, self.library)
        self.assertTrue(old.exists())

    def test_path_resolution_rejects_legacy_traversal_and_escaping_symlinks(self):
        _, _, _, path, _ = self.seed(legacy=False, published=False)
        row = dict(self.db.execute("SELECT * FROM media").fetchone())
        self.assertEqual(store.media_path(row, self.library), path.resolve())
        for bad in (str(path), "../" + path.name):
            with self.subTest(path=bad), self.assertRaises(ValueError):
                store.media_path(dict(row, path=bad), self.library)
        outside = Path(self.temp.name) / "outside.jpg"
        path.rename(outside)
        path.symlink_to(outside)
        with self.assertRaises(ValueError):
            store.media_path(row, self.library)
        with self.assertRaises(ValueError):
            media.migrate(self.db, self.library)

    def test_draft_resume_and_export_work_after_moving_media(self):
        from triviadb.pipeline import generate, review_pending, media_for
        from triviadb.__main__ import export_db
        fid, mid, _, _, sha = self.seed(legacy=False, published=False)
        with self.db:
            self.db.execute("DELETE FROM candidates")
            for i in range(2, 5):
                store.add_fact(self.db, fact(subject=f"wd:Q{i}", answer=f"wd:Q{10+i}", label=f"Director {i}"))
        root, stages = self.library, []
        test = self
        class Client:
            def complete(self, stage, model, prompt, schema, attachments=()):
                stages.append(stage)
                test.assertEqual(Path(attachments[0]["path"]).parent, root.resolve())
                test.assertEqual(hashlib.sha256(Path(attachments[0]["path"]).read_bytes()).hexdigest(), sha)
                item = dict(approval(), fact_id=fid) if stage == "review" else dict(
                    fact_id=fid, question="Which filmmaker is pictured?", notes=None, difficulty=4, skip_reason="")
                return {"items": [item]}
        generate(self.db, Client(), limit=1, draft_only=True, question_type="photo", media_dir=root)
        moved = Path(self.temp.name) / "moved-library"
        root.rename(moved)
        root = moved
        review_pending(self.db, Client(), limit=1, media_dir=root)
        self.assertEqual(stages, ["write", "review"])
        self.assertEqual(tuple(self.db.execute("SELECT id,media_id FROM questions").fetchone()), (mid, mid))
        target = Path(self.temp.name) / "game.sqlite"
        export_db(self.db, target, media_dir=root)
        relocated = Path(self.temp.name) / "unrelated-folder"
        relocated.mkdir()
        target.rename(relocated / target.name)
        target.with_suffix(".media").rename(relocated / "game.media")
        with closing(store.connect(relocated / target.name)) as exported:
            row = exported.execute("SELECT * FROM media").fetchone()
            self.assertEqual(row["path"], mid + ".jpg")
            attachment = media_for(exported, fid, media_dir=relocated / "game.media")
            self.assertEqual(hashlib.sha256(Path(attachment["path"]).read_bytes()).hexdigest(), sha)
            export_db(exported, relocated / "again.sqlite", media_dir=relocated / "game.media")
