# Shared Question/Media IDs Implementation Plan

> **REQUIRED SUB-SKILL:** Use the executing-plans skill to implement this plan task-by-task.

**Goal:** Use one UUID for a media question and its attachment, store filename-only media paths, safely migrate the existing library and export, and exclude content only for confirmed rights problems.

**Architecture:** A media import reserves the eventual question UUID. Approval uses that UUID rather than creating another one. Keep the existing nullable `questions.media_id` foreign-key field for compatibility and referential integrity, but its value must equal `questions.id` for media questions. One asset record belongs to one question/fact. Retain SHA-256 for integrity and deduplication. Resolve filenames through one shared helper against the operation's `--media-dir`; never persist the resolved absolute asset path.

**Tech Stack:** Existing Python standard library, SQLite, unittest, optional ffmpeg. Work directly on `main`, as explicitly requested. No worktree, dependencies, or new model calls required.

---

## Task 1: Shared identity, portable paths, and migration

**Files:** Modify `triviadb/store.py`, `triviadb/media.py`, `triviadb/pipeline.py`, `triviadb/__main__.py`, `tests/test_triviadb.py`; create `tests/test_media_paths.py`.

1. Add failing tests for UUID filenames, shared question/media IDs, reuse rejection, portable generation/review/export, and migration failure/restart handling. Run targeted tests and confirm failures stem from missing behavior.
2. Add shared filename/path validation helpers. Require canonical UUIDs and `.jpg`/`.wav`; reject traversal, legacy stored paths outside migration, and symlinks escaping the configured library.
3. New imports write `<reserved-question-uuid>.jpg/.wav`; the existing checksum column still detects duplicate content. Prevent attachment of one media record to multiple facts. Approval uses the reserved media UUID; text questions still receive fresh UUIDs.
4. Thread the configured media directory through selection/review/export. Resolve attachment paths only in memory before Gemini reads them. Export the same UUID filenames into `<export-stem>.media`, storing only filenames in the exported media rows.
5. Add `media migrate`. Preflight every row and file before mutations: UUIDs, checksums, destination collisions, missing files, and shared media. For published questions, use the existing question UUID; for unpublished media, retain the reserved UUID. Preserve all question/fact IDs, checksums, source evidence, and review history.
6. Migration creates new filenames using same-directory hard links before committing SQLite updates, defers foreign-key checks during ID remapping, and only removes verified old hash aliases after commit. A failed transaction leaves original files and rows usable; reruns reuse verified new files. A rerun after commit also removes leftover hash aliases. Refuse unexpected files instead of overwriting them.
7. Update active references in `media`, `fact_media`, `questions.media_id`, candidate payloads, and media-specific normalized-question keys. Do not rewrite historical model responses. Repeating migration must be harmless.
8. Run `python3 -m unittest discover -s tests -v` and `python3 -m compileall -q triviadb`; require all tests to pass.

## Task 2: Existing data migration and rights audit

**Files:** Existing ignored `data/trivia.sqlite`, `data/media/`, `data/party.sqlite`, `data/party.media/`; backups and audit report under ignored `data/backups/` and `data/rights-review.json`.

1. Under writer locks, use SQLite's backup API and copy the small media folders to a uniquely named backup directory. Record question IDs, fact/candidate counts, media checksums, and an `.env` checksum without revealing secrets.
2. Audit all fact provenance licenses and all six asset-level Met API snapshots. Compare object IDs, `isPublicDomain`, official image hosts, source URLs, and license records. Flag uncertainty separately; it is not proof that usage is prohibited. Do not infer image rights from metadata licensing alone.
3. No additional import-license policy expansion is requested: existing CC0/public-domain source selection remains. Other license names or uncertainty are not by themselves proof of infringement. Remove existing content for rights only if affirmative evidence establishes the problem; otherwise retain it.
4. Run `python3 -m triviadb media migrate` for the working library and `python3 -m triviadb --db data/party.sqlite --media-dir data/party.media media migrate` for the existing export. Run each twice to verify restart/idempotence.
5. Confirm all existing question IDs and counts are unchanged, media paths are basenames, filenames match media IDs, and every media question's ID equals its media ID. Confirm old hash aliases are gone from active libraries and backups retain recoverable originals.

## Task 3: Verification and documentation

**Files:** Update `README.md` and this plan's verification section.

1. Update naming, shared-ID semantics, migration commands, configured-root handling, export resolution, backup instructions, and rights-audit limitations.
2. Verify both databases using `PRAGMA integrity_check` and `PRAGMA foreign_key_check`; verify every media checksum and active reference.
3. Copy the export plus its asset folder to an unrelated temporary directory and verify media resolution/re-export there with an explicit `--media-dir`.
4. Run the full offline suite again and inspect the actual result. Review `git diff --check`, the full diff, secret exclusions, and changed-file status. No automatic generation, rights-based deletions without evidence, worktrees, or remote push.

## Verification results

- Implemented directly on `main`; no worktree, new dependency, or Gemini request.
- 41 offline tests pass, including shared IDs, relocation, migration collision/preflight checks, SQLite failure/retry, post-commit alias cleanup, and symlink rejection. Compile check and `git diff --check` pass.
- Working library: six records/files migrated; existing export: one record/file migrated. Both repeated migrations reported zero further changes.
- All 16 published question IDs and question contents were preserved (only the compatibility media link changed). Facts, provenance, statuses, review history, cached model jobs, quotas/checkpoints, asset bytes, licenses, and checksums were compared against backups and preserved.
- Both SQLite integrity/foreign-key checks pass. A relocated real export was successfully read and re-exported using an explicit asset directory.
- Recovery backup: `data/backups/shared-media-ids-0l7e6fo7/` (SQLite backup API plus copied asset folders). Original hash filenames remain only in backups/source caches, not active media libraries.
- Rights audit: 717 source-fact records and all six distinct Met images have the expected recorded CC0/public-domain evidence. Asset object IDs, official image URLs, and `isPublicDomain=true` agree with the saved official API snapshots. Zero uncertain evidence records or confirmed rights violations were found; no rights-based content exclusions were made. See `data/rights-review.json`.
- `data/pilot.json` was refreshed to use shared IDs. `.env` checksum is unchanged; secrets and local data remain Git-ignored. Nothing is staged, committed, or pushed for this change.
- Independent read-only review found no issues and returned an OK verdict. The reviewer inspected source/tests and the parent-run test log; execution evidence above is parent-verified.
