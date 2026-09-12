import copy
import json
import sqlite3
import shutil
import tempfile
import wave
import unittest
import uuid
from contextlib import closing
from pathlib import Path

from triviadb import store


def fact(subject="wd:Q1", answer="wd:Q10", label="Director One", **extra):
    return dict(subject_id=subject, subject="A Famous Movie", predicate="P57",
                object_id=answer, answer=label, category="Movies", subcategory="Movie Directors",
                pool="film-director", popularity=50, qualifiers={}, context={"year": 1975},
                source="wikidata", source_record_id=subject, source_url="https://www.wikidata.org/wiki/Q1",
                license="CC0-1.0", snapshot="test.json", **extra)


def question():
    return dict(category="Movies", subcategory="Movie Directors",
                question="Who directed the 1975 film A Famous Movie?", a="Director One",
                b="Director Two", c="Director Three", d="Director Four", difficulty=4, notes=None,
                type="text", media_id=None)


def approval():
    return dict(verdict="accept", checks={key: True for key in store.REVIEW_CHECKS},
                difficulty=4, reason="Supported by the supplied record and suitable for pub play.")


class DatabaseTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "trivia.sqlite"
        self.db = store.connect(self.path)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()


class StoreTests(DatabaseTest):
    def test_identity_ignores_source_and_canonicalizes_entity_urls(self):
        first = fact()
        first["qualifiers"] = {"region": ["US", "CA"], "year": 1975}
        second = dict(first, subject_id="https://www.wikidata.org/wiki/Q1",
                      object_id="http://www.wikidata.org/entity/Q10", source="met",
                      source_record_id="123", source_url="https://www.metmuseum.org/art/collection/search/123",
                      qualifiers={"year": 1975, "region": ["CA", "US"]})
        with self.db:
            a = store.add_fact(self.db, first)
            b = store.add_fact(self.db, second)
        self.assertEqual(a, b)
        self.assertEqual(self.db.execute("SELECT count(*) FROM facts").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM provenance").fetchone()[0], 2)
        self.db.close()
        self.db = store.connect(self.path)
        self.assertEqual(self.db.execute("SELECT id FROM facts").fetchone()[0], a)

    def test_conflicting_sources_quarantine_instead_of_choosing_an_answer(self):
        with self.db:
            fid = store.add_fact(self.db, fact())
            store.save_candidate(self.db, fid, question(), "writer")
            store.apply_review(self.db, fid, approval(), "reviewer")
        self.assertEqual(self.db.execute("SELECT count(*) FROM questions").fetchone()[0], 1)
        with self.db:
            store.add_fact(self.db, dict(fact(), answer="Someone Else", object_id="wd:Q99"))
        self.assertEqual(self.db.execute("SELECT count(*) FROM questions").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT count(*) FROM facts WHERE status='conflict'").fetchone()[0], 2)
        self.assertEqual(self.db.execute("SELECT status FROM candidates WHERE fact_id=?", (fid,)).fetchone()[0], "conflict")

    def test_only_strict_review_creates_question_with_exact_schema(self):
        with self.db:
            fid = store.add_fact(self.db, fact())
            store.save_candidate(self.db, fid, question(), "writer")
        self.assertEqual(self.db.execute("SELECT count(*) FROM questions").fetchone()[0], 0)
        bad = approval()
        bad["checks"]["one_correct"] = "true"
        with self.assertRaises(ValueError), self.db:
            store.apply_review(self.db, fid, bad, "reviewer")
        with self.db:
            store.apply_review(self.db, fid, approval(), "reviewer")
            store.apply_review(self.db, fid, approval(), "reviewer")
        row = dict(self.db.execute("SELECT * FROM questions").fetchone())
        self.assertEqual(set(row), {"id", *question()})
        self.assertEqual(uuid.UUID(row["id"]).version, 4)
        self.assertEqual(self.db.execute("SELECT count(*) FROM questions").fetchone()[0], 1)

    def test_validation_rejects_invalid_answers_difficulty_category_and_time(self):
        for patch in ({"b": "DIRECTOR ONE!"}, {"difficulty": True}, {"difficulty": 10},
                      {"question": "Who is the current director?"}, {"question": "Did Director One direct this?"},
                      {"category": "Unlisted"}, {"notes": ["not text"]}, {"a": "Made Up"}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                store.validate_question(dict(question(), **patch), fact())

    def test_text_deduplication_catches_different_facts(self):
        with self.db:
            for subject in ("wd:Q1", "wd:Q2"):
                fid = store.add_fact(self.db, fact(subject=subject))
                store.save_candidate(self.db, fid, question(), "writer")
                store.apply_review(self.db, fid, approval(), "reviewer")
        self.assertEqual(self.db.execute("SELECT count(*) FROM questions").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM candidates WHERE status='rejected'").fetchone()[0], 1)

    def test_calculated_comparisons_with_different_choices_are_not_text_duplicates(self):
        with self.db:
            for index, wrong in enumerate(("Director Two", "Director Five")):
                q = dict(question(), b=wrong)
                fid = store.add_fact(self.db, fact(subject=f"Q{index+1}", fixed_options=[q[k] for k in "abcd"]))
                store.save_candidate(self.db, fid, q, "writer")
                store.apply_review(self.db, fid, approval(), "reviewer")
        self.assertEqual(self.db.execute("SELECT count(*) FROM questions").fetchone()[0], 2)

    def test_rollback_and_checkpoint_are_atomic(self):
        with self.assertRaises(RuntimeError), self.db:
            store.add_fact(self.db, fact())
            store.checkpoint(self.db, "import", {"cursor": 5})
            raise RuntimeError("simulated interruption")
        self.assertEqual(self.db.execute("SELECT count(*) FROM facts").fetchone()[0], 0)
        self.assertEqual(store.checkpoint(self.db, "import"), {})

    def test_non_cc0_fact_is_not_imported(self):
        with self.assertRaises(ValueError), self.db:
            store.add_fact(self.db, dict(fact(), license="CC-BY-NC-4.0"))


class SourceTests(DatabaseTest):
    def test_wikidata_skips_multi_answer_unknown_and_qualified_claims(self):
        from triviadb.sources import extract_wikidata
        statement = {"rank": "normal", "mainsnak": {"snaktype": "value", "datavalue": {"type": "wikibase-entityid", "value": {"id": "Q10"}}}}
        entity = {"id": "Q1", "labels": {"en": {"value": "A Famous Movie"}},
                  "descriptions": {"en": {"value": "1975 American film"}},
                  "claims": {"P57": [statement]}, "sitelinks": {"enwiki": {"title": "A Famous Movie"}}}
        labels = {"Q10": {"labels": {"en": {"value": "Director One"}}}}
        self.assertIsNotNone(extract_wikidata(entity, "film-director", labels, "cache.json"))
        for change in ({"qualifiers": {"P580": []}}, {"mainsnak": {"snaktype": "somevalue"}}):
            bad = copy.deepcopy(entity)
            bad["claims"]["P57"].append(dict(statement, **change))
            self.assertIsNone(extract_wikidata(bad, "film-director", labels, "cache.json"))
        bad = copy.deepcopy(entity)
        second = copy.deepcopy(statement)
        second["mainsnak"]["datavalue"]["value"]["id"] = "Q11"
        bad["claims"]["P57"].append(second)
        self.assertIsNone(extract_wikidata(bad, "film-director", labels, "cache.json"))

    def test_met_only_confident_single_artist_and_shared_ids(self):
        from triviadb.sources import extract_met
        row = {"Object ID": "123", "Title": "A Painting", "Is Highlight": "True",
               "Artist Display Name": "An Artist", "Artist Role": "Artist", "Artist Prefix": "",
               "Artist Suffix": "", "Artist Wikidata URL": "https://www.wikidata.org/wiki/Q10",
               "Object Wikidata URL": "https://www.wikidata.org/wiki/Q1", "Classification": "Paintings"}
        good = extract_met(row, "met.csv")
        self.assertEqual(good["predicate"], "P170")
        self.assertEqual(store.entity_id(good["subject_id"]), "wd:Q1")
        for patch in ({"Artist Prefix": "Attributed to"}, {"Artist Display Name": "One|Two"},
                      {"Is Highlight": "False"}, {"Artist Display Name": "Unknown"}):
            self.assertIsNone(extract_met(dict(row, **patch), "met.csv"))

    def test_geography_uses_math_and_stable_nonoverlapping_groups(self):
        from triviadb.sources import natural_facts
        features = [{"properties": {"WIKIDATAID": f"Q{i}", "NAME_EN": f"City {i}", "ADM0NAME": "United States of America",
                                     "ADM0_A3": "USA", "ADM1NAME": "State", "POP_MAX": 500000, "LATITUDE": lat}}
                    for i, lat in enumerate((10, 20, 30, 40, 50, 60, 70, 80), 1)]
        results = list(natural_facts({"features": features}, "earth.json"))
        reverse = list(natural_facts({"features": list(reversed(features))}, "earth.json"))
        self.assertEqual(results, reverse)
        self.assertEqual(len(results), 2)
        all_ids = []
        for item in results:
            cities = item["context"]["cities"]
            self.assertEqual(item["object_id"], max(cities, key=lambda c: c["latitude"])["id"])
            all_ids.extend(city["id"] for city in cities)
        self.assertEqual(len(all_ids), len(set(all_ids)))

    def test_options_are_repeatable_and_exclude_correct_aliases(self):
        from triviadb.sources import choose_options
        with self.db:
            for i in range(12):
                store.add_fact(self.db, fact(subject=f"wd:Q{i+100}", answer=f"wd:Q{i+200}", label=f"Director {i}"))
            fid = store.add_fact(self.db, fact())
        f = store.unpack(self.db.execute("SELECT * FROM facts WHERE id=?", (fid,)).fetchone())
        a = choose_options(self.db, f)
        self.assertEqual(a, choose_options(self.db, f))
        self.assertEqual(a[0], f["answer"])
        self.assertEqual(len(set(a)), 4)

    def test_distractor_bound_counts_distinct_answers_not_repeated_facts(self):
        from triviadb.sources import choose_options
        with self.db:
            fid = store.add_fact(self.db, fact())
            for i in range(512):
                store.add_fact(self.db, dict(fact(subject=f"Q{i+100}", answer="Q900", label="Prolific Director"), popularity=100))
            for i in range(2):
                store.add_fact(self.db, dict(fact(subject=f"Q{i+1000}", answer=f"Q{i+2000}", label=f"Other Director {i}"), popularity=5))
        f = store.unpack(self.db.execute("SELECT * FROM facts WHERE id=?", (fid,)).fetchone())
        self.assertEqual(len(choose_options(self.db, f) or []), 4)

    def test_source_resume_prioritizes_unvisited_recipes_after_a_partial_run(self):
        from triviadb import sources
        from unittest.mock import Mock
        from urllib.parse import unquote
        from triviadb.net import Paused
        first_key = "wikidata:" + store.digest(["film-director", 25, None])
        with self.db:
            store.checkpoint(self.db, first_key, {"after": "http://www.wikidata.org/entity/Q100", "done": False})
        http = Mock()
        http.json.side_effect = Paused("Server cooldown")
        with self.assertRaises(Paused):
            sources.import_wikidata(self.db, http, ["film-director", "book-author"])
        self.assertIn("wd:Q7725634", unquote(http.json.call_args.args[0]))
        self.assertEqual(http.json.call_count, 1)

    def test_book_recipe_uses_current_wikidata_literary_work_class(self):
        from triviadb.sources import matching_recipes
        entity = {"claims": {"P31": [{"mainsnak": {"snaktype": "value", "datavalue": {"type": "wikibase-entityid", "value": {"id": "Q7725634"}}}}]}}
        self.assertIn("book-author", matching_recipes(entity))

    def test_local_dump_resume_and_label_pass(self):
        from triviadb.sources import import_dump
        path = Path(self.temp.name) / "tiny.json"
        entities = [
            {"id": "Q10", "labels": {"en": {"value": "Director One"}}},
            {"id": "Q1", "labels": {"en": {"value": "A Famous Movie"}},
             "descriptions": {"en": {"value": "1975 American film"}},
             "sitelinks": {"enwiki": {"title": "A Famous Movie"}},
             "claims": {"P31": [{"mainsnak": {"snaktype": "value", "datavalue": {"value": {"id": "Q11424"}, "type": "wikibase-entityid"}}}],
                        "P57": [{"mainsnak": {"snaktype": "value", "datavalue": {"value": {"id": "Q10"}, "type": "wikibase-entityid"}}}]}}]
        path.write_text("[\n" + ",\n".join(json.dumps(e) for e in entities) + "\n]\n")
        import_dump(self.db, path, min_sitelinks=1)
        import_dump(self.db, path, min_sitelinks=1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM facts").fetchone()[0], 1)


class MediaTests(DatabaseTest):
    def test_question_type_requires_correct_media_reference(self):
        with self.assertRaises(ValueError):
            store.validate_question(dict(question(), type="photo"), fact())
        with self.assertRaises(ValueError):
            store.validate_question(dict(question(), media_id="fake"), fact())

    def test_media_question_must_not_supply_the_work_title_in_text(self):
        with self.assertRaises(ValueError):
            store.validate_question(dict(question(), type="photo", media_id="asset"), fact())

    def test_license_and_rights_confirmation_fail_closed(self):
        from triviadb.media import import_asset
        for license_id, confirmed in (("CC-BY-NC-4.0", True), ("CC0-1.0", False)):
            with self.assertRaises(ValueError):
                import_asset(self.db, Path("missing.wav"), Path(self.temp.name), kind="sound",
                             license_id=license_id, rights_confirmed=confirmed,
                             source_url="https://example.org/sound", license_url="https://example.org/license",
                             creator="Test creator", evidence="Recording is CC0", seconds=0.1)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "optional ffmpeg not installed")
    def test_audio_clipping_content_hash_storage_cap_and_attachment(self):
        from triviadb.media import import_asset, attach
        source = Path(self.temp.name) / "answer-is-director-one.wav"
        with wave.open(str(source), "wb") as out:
            out.setparams((1, 2, 44100, 0, "NONE", "not compressed"))
            out.writeframes(b"\0\0" * 44100)
        kw = dict(kind="sound", license_id="CC0-1.0", rights_confirmed=True,
                  source_url="https://example.org/sound", license_url="https://example.org/license",
                  creator="Test creator", evidence="Test recording explicitly CC0", seconds=0.1)
        mid = import_asset(self.db, source, Path(self.temp.name) / "assets", **kw)
        self.assertEqual(mid, import_asset(self.db, source, Path(self.temp.name) / "assets", **kw))
        asset = dict(self.db.execute("SELECT * FROM media WHERE id=?", (mid,)).fetchone())
        self.assertEqual(asset["path"], mid + ".wav")
        with wave.open(str(Path(self.temp.name) / "assets" / asset["path"]), "rb") as audio:
            self.assertAlmostEqual(audio.getnframes() / audio.getframerate(), 0.1, places=2)
        with self.assertRaises(ValueError):
            import_asset(self.db, source, Path(self.temp.name) / "tiny", **dict(kw, seconds=0.2), max_bytes=1)
        with self.db:
            fid = store.add_fact(self.db, fact())
        attach(self.db, fid, mid)
        q = dict(question(), question="Which filmmaker is identified by this audio clip?", type="sound", media_id=mid)
        with self.db:
            store.save_candidate(self.db, fid, q, "writer")
            store.apply_review(self.db, fid, approval(), "reviewer", media_dir=Path(self.temp.name) / "assets")
        self.assertEqual(self.db.execute("SELECT type FROM questions").fetchone()[0], "sound")
        self.assertEqual(self.db.execute("SELECT id FROM questions").fetchone()[0], mid)
        from triviadb.__main__ import export_db
        destination = Path(self.temp.name) / "game.sqlite"
        export_db(self.db, destination, media_dir=Path(self.temp.name) / "assets")
        with closing(sqlite3.connect(destination)) as game:
            asset_path = game.execute("SELECT path FROM media").fetchone()[0]
            self.assertEqual(asset_path, mid + ".wav")
            self.assertTrue((destination.parent / "game.media" / asset_path).is_file())
        with self.assertRaises(ValueError):
            attach(self.db, fid, mid)


if __name__ == "__main__":
    unittest.main()
