import unittest
from contextlib import closing

from triviadb import store
from test_triviadb import DatabaseTest, fact


class NetworkTests(DatabaseTest):
    def test_source_retry_after_is_persisted_and_resume_does_not_hammer_server(self):
        import time
        from pathlib import Path
        from unittest.mock import patch
        from triviadb.net import CachedHTTP, HTTPFailure
        from triviadb.gemini import Paused
        client = CachedHTTP(self.db, Path(self.temp.name) / "cache")
        with patch("triviadb.net.download", side_effect=HTTPFailure(429, retry_after="120")) as download:
            with self.assertRaises(Paused):
                client.get("https://example.org/data")
            with self.assertRaises(Paused):
                client.get("https://example.org/data")
            self.assertEqual(download.call_count, 1)
        state = store.checkpoint(self.db, "http:example.org")
        self.assertGreater(state["next_at"], time.time() + 100)

    def test_partial_download_is_not_promoted_to_cache(self):
        import io
        from pathlib import Path
        from unittest.mock import patch
        from triviadb.net import download
        response = io.BytesIO(b"partial")
        response.headers = {"Content-Length": "100"}
        target = Path(self.temp.name) / "cache.json"
        with patch("triviadb.net.open_url", return_value=response), self.assertRaises(ValueError):
            download("https://example.org/data", target)
        self.assertFalse(target.exists())
        self.assertTrue(target.with_name(target.name + ".part").exists())


class GeminiTests(DatabaseTest):
    def test_round_robin_cache_and_persistent_quota_failover(self):
        from triviadb.gemini import Gemini
        from triviadb.net import HTTPFailure
        seen = []
        def transport(url, data=None, headers=None):
            seen.append(headers["x-goog-api-key"])
            if len(seen) == 1:
                raise HTTPFailure(429, {"error": {"details": [{"retryDelay": "120s"}]}}, "90")
            return {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": '{"value":"ok"}'}]}}]}
        client = Gemini(self.db, ["secret-one", "secret-two"], transport=transport, interval=0)
        self.assertEqual(client.complete("write", "test-model", "prompt", {"type": "object"}), {"value": "ok"})
        self.assertEqual(seen, ["secret-one", "secret-two"])
        client.complete("write", "test-model", "prompt", {"type": "object"})
        self.assertEqual(len(seen), 2)
        self.db.close()
        self.db = store.connect(self.path)
        client = Gemini(self.db, ["secret-one", "secret-two"], transport=transport, interval=0)
        client.complete("write", "test-model", "different", {"type": "object"})
        self.assertEqual(seen[-1], "secret-two")
        for table in ("key_state", "jobs", "api_attempts"):
            self.assertNotIn("secret-one", str([tuple(row) for row in self.db.execute(f"SELECT * FROM {table}")]))

    def test_same_project_cooldown_and_permanent_error_stop(self):
        from triviadb.gemini import Gemini, Paused
        from triviadb.net import HTTPFailure
        for code in (429, 404):
            seen = []
            def transport(*args, **kwargs):
                seen.append(1)
                raise HTTPFailure(code)
            client = Gemini(self.db, ["one", "two"], projects=["shared", "shared"], transport=transport, interval=0)
            with self.assertRaises((Paused, RuntimeError)):
                client.complete("write", "model" + str(code), "prompt", {"type": "object"})
            self.assertEqual(len(seen), 1)
            self.assertEqual(self.db.execute("SELECT code FROM api_attempts ORDER BY id DESC LIMIT 1").fetchone()[0], code)

    def test_model_output_schema_is_locally_checked_and_bad_json_not_cached(self):
        from triviadb.gemini import Gemini
        def transport(*args, **kwargs):
            return {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": '{"value":true}'}]}}]}
        client = Gemini(self.db, ["key"], transport=transport, interval=0)
        with self.assertRaises(ValueError):
            client.complete("write", "model", "prompt", {"type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"]})
        self.assertEqual(self.db.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)

    def test_blank_review_reason_is_not_cached_and_retry_can_replace_it(self):
        import json
        from triviadb.gemini import Gemini
        from triviadb.pipeline import schema_for
        from test_triviadb import approval
        seen = []
        def transport(*args, **kwargs):
            seen.append(1)
            item = dict(approval(), fact_id="f", reason=" " if len(seen) == 1 else "Source supports this question")
            return {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": json.dumps({"items": [item]})}]}}]}
        client = Gemini(self.db, ["key"], transport=transport, interval=0)
        with self.assertRaises(ValueError):
            client.complete("review", "model", "prompt", schema_for(["f"], review=True))
        self.assertEqual(self.db.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)
        output = client.complete("review", "model", "prompt", schema_for(["f"], review=True))
        self.assertEqual(output["items"][0]["reason"], "Source supports this question")
        self.assertEqual(len(seen), 2)

    def test_request_budget_includes_failed_requests(self):
        from triviadb.gemini import Gemini, Paused
        from triviadb.net import HTTPFailure
        def transport(*args, **kwargs):
            raise HTTPFailure(503)
        client = Gemini(self.db, ["one", "two"], transport=transport, interval=0, max_calls=1)
        with self.assertRaises(Paused):
            client.complete("write", "model", "prompt", {"type": "object"})
        self.assertEqual(self.db.execute("SELECT count(*) FROM api_attempts").fetchone()[0], 1)


class PipelineTests(DatabaseTest):
    def test_selection_pages_past_ineligible_high_popularity_facts(self):
        from triviadb.pipeline import select_batch
        with self.db:
            for i in range(256):
                store.add_fact(self.db, dict(fact(subject=f"Q{i+100}", answer="Q900", label="Only answer"), pool="scarce", popularity=100))
            for i in range(4):
                store.add_fact(self.db, dict(fact(subject=f"Q{i+1000}", answer=f"Q{i+2000}", label=f"Valid {i}"), pool="enough", popularity=5))
        self.assertEqual(len(select_batch(self.db, 1, set(), category="Movies")), 1)

    def test_drafts_resume_without_rewriting_and_failed_review_does_not_publish(self):
        from triviadb.pipeline import generate
        seen = []
        class Client:
            def complete(inner, stage, model, prompt, schema, attachments=()):
                ids = schema["properties"]["items"]["items"]["properties"]["fact_id"]["enum"]
                seen.append(stage)
                if stage == "write":
                    return {"items": [dict(fact_id=fid, question="Who directed the 1975 film A Famous Movie?", difficulty=4, notes=None, skip_reason="") for fid in ids]}
                raise RuntimeError("simulated quota interruption")
        with self.db:
            store.add_fact(self.db, fact())
            for i in range(3):
                store.add_fact(self.db, fact(subject=f"wd:Q{i+100}", answer=f"wd:Q{i+200}", label=f"Director {i+2}"))
        with self.assertRaises(RuntimeError):
            generate(self.db, Client(), limit=1, batch_size=1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM questions").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT count(*) FROM candidates WHERE status='draft'").fetchone()[0], 1)
        with self.assertRaises(RuntimeError):
            generate(self.db, Client(), limit=1, batch_size=1)
        self.assertEqual(seen.count("write"), 1)


class CLITests(DatabaseTest):
    def test_samples_are_seeded_category_balanced_and_use_party_difficulty_mix(self):
        from collections import Counter
        from triviadb.__main__ import sample_rows
        from test_triviadb import question, approval
        serial = 10000
        with self.db:
            for cat in ("Movies", "Music", "Geography"):
                for difficulty in (2, 5, 8):
                    for _ in range(13):
                        serial += 1
                        f = dict(fact(subject=f"Q{serial}"), category=cat)
                        fid = store.add_fact(self.db, f)
                        q = dict(question(), category=cat, question=f"Who created fixture item {serial}?", difficulty=difficulty)
                        store.save_candidate(self.db, fid, q, "writer")
                        store.apply_review(self.db, fid, dict(approval(), difficulty=difficulty), "reviewer")
        rows = sample_rows(self.db, limit=30, seed=42)
        self.assertEqual(rows, sample_rows(self.db, limit=30, seed=42))
        self.assertNotEqual(rows, sample_rows(self.db, limit=30, seed=99))
        self.assertEqual(Counter(q["category"] for q in rows), {"Movies": 10, "Music": 10, "Geography": 10})
        self.assertEqual(Counter(q["difficulty"] for q in rows), {2: 15, 5: 12, 8: 3})
        self.assertEqual(len({q["id"] for q in rows}), 30)
        self.assertTrue(all(q["difficulty"] <= 3 for q in sample_rows(self.db, limit=30, seed=42, max_difficulty=3)))

    def test_export_contains_only_approved_rows_and_preserves_provenance(self):
        from triviadb.__main__ import export_db
        from test_triviadb import question, approval
        from pathlib import Path
        import sqlite3
        with self.db:
            fid = store.add_fact(self.db, fact())
            store.save_candidate(self.db, fid, question(), "writer")
            store.apply_review(self.db, fid, approval(), "reviewer")
            other = store.add_fact(self.db, fact(subject="Q22"))
            store.save_candidate(self.db, other, question(), "writer")
        output = Path(self.temp.name) / "game.sqlite"
        export_db(self.db, output)
        with closing(sqlite3.connect(output)) as game:
            self.assertEqual(game.execute("SELECT count(*) FROM questions").fetchone()[0], 1)
            self.assertEqual(game.execute("SELECT count(*) FROM facts").fetchone()[0], 1)
            self.assertEqual(game.execute("SELECT count(*) FROM candidates").fetchone()[0], 0)
            self.assertEqual(game.execute("SELECT count(*) FROM provenance").fetchone()[0], 1)
            self.assertEqual(game.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(game.execute("PRAGMA foreign_key_check").fetchall(), [])
        with self.assertRaises(ValueError):
            export_db(self.db, output)

    def test_manual_difficulty_tuning_preserves_original_model_review(self):
        from triviadb.__main__ import set_difficulty
        from test_triviadb import question, approval
        with self.db:
            fid = store.add_fact(self.db, fact())
            store.save_candidate(self.db, fid, question(), "writer")
            store.apply_review(self.db, fid, approval(), "reviewer")
        qid = self.db.execute("SELECT id FROM questions").fetchone()[0]
        set_difficulty(self.db, qid, 8, "Requires specialist recall")
        self.assertEqual(self.db.execute("SELECT difficulty FROM questions").fetchone()[0], 8)
        self.assertIn("prior_review", self.db.execute("SELECT review FROM candidates").fetchone()[0])
        with self.assertRaises(ValueError):
            set_difficulty(self.db, qid, 10, "Invalid")

    def test_manual_rejection_unpublishes_but_retains_fact_and_review_evidence(self):
        from triviadb.__main__ import reject_question
        from test_triviadb import question, approval
        with self.db:
            fid = store.add_fact(self.db, fact())
            store.save_candidate(self.db, fid, question(), "writer")
            store.apply_review(self.db, fid, approval(), "reviewer")
        qid = self.db.execute("SELECT id FROM questions").fetchone()[0]
        reject_question(self.db, qid, "Too obscure for a pub audience")
        self.assertEqual(self.db.execute("SELECT count(*) FROM questions").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT count(*) FROM facts").fetchone()[0], 1)
        candidate = self.db.execute("SELECT * FROM candidates").fetchone()
        self.assertEqual(candidate["status"], "rejected")
        self.assertIn("Too obscure", candidate["reason"])
        self.assertIn("prior_review", candidate["review"])

    def test_cli_help_and_init_work_without_api_keys(self):
        from triviadb.__main__ import main
        from pathlib import Path
        with self.assertRaises(SystemExit) as result:
            main(["--help"])
        self.assertEqual(result.exception.code, 0)
        self.assertEqual(main(["--db", str(Path(self.temp.name) / "cli.sqlite"), "init"]), 0)


if __name__ == "__main__":
    unittest.main()
