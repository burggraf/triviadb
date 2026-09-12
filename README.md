# TriviaDB

A restartable CLI for building **original, source-grounded American pub trivia** in SQLite. Text, photo, and sound questions share one schema. Gemini writes and reviews; it does not invent the source facts or answer choices.

Python **3.11+**, macOS/Linux. No Python runtime dependencies. Optional media processing needs `ffmpeg` and `ffprobe` (`brew install ffmpeg` on macOS).

## Quick start

From this directory, with your existing `.env`:

```sh
python3 -m triviadb init
python3 -m triviadb import natural-earth
python3 -m triviadb import met
python3 -m triviadb import wikidata --recipes all --pages 1

# Optional: attach Open Access photos BEFORE generating their questions.
python3 -m triviadb media met --limit 10

# Start small. This is an attempt limit, NOT a promise of 30 acceptances.
python3 -m triviadb generate --limit 30 --max-calls 20
python3 -m triviadb stats
python3 -m triviadb sample --limit 30 --seed 42
```

The working database is **`data/trivia.sqlite`**. Downloads are in `data/cache/`; sanitized assets are in `data/media/`. Your `.env` is not changed. These paths and secrets are excluded by `.gitignore`.

No install is required. Optionally, `pip install -e .` provides a `triviadb` executable.

Run `python3 -m triviadb --help`, or append `--help` to any command. Global path options go **before** the command:

```sh
python3 -m triviadb --db /path/to/trivia.sqlite --media-dir /path/to/assets stats
```

## Sources and licenses

| Implemented source | Access | What becomes trivia | License |
|---|---|---|---|
| [Wikidata](https://www.wikidata.org/wiki/Wikidata:Licensing) | Cached entity API + bounded SPARQL discovery; local dump importer | Film directors/scores, books, albums, TV creators, art, elements, games, landmarks, and other catalog recipes | Structured data: CC0 |
| [The Met](https://github.com/metmuseum/openaccess) | Official CSV download, roughly 330 MB; optional object/image API | Highlighted paintings, sculpture, drawings and prints with confident single-artist attribution | Dataset: CC0; images checked separately |
| [Natural Earth](https://www.naturalearthdata.com/about/terms-of-use/) | Official populated-places GeoJSON download | Northernmost-of-four city comparisons, calculated from coordinates | Public domain |

`python3 -m triviadb sources` lists the implemented recipes and their categories. Defined major categories: Movies, Television, Music, Books & Language, History, Geography, Science & Technology, Nature, Sports & Games, Arts & Culture, Food & Drink, and Space. Recipe subcategories are reusable topics, not individual people or works.

**A source catalog is not a list of guaranteed available questions.** Some exact Wikidata classes are sparse, facts may be ambiguous, or a pool may lack three suitable answers. Those are skipped rather than filled with inventions. All twelve categories need not be represented in a small initial import.

MusicBrainz, Smithsonian, GeoNames, and other adapters are **not implemented yet**. No Jeopardy clues, scraped quiz wording, Wikipedia prose, or noncommercial/share-alike question banks are imported. The software's license checks are not legal advice or a guarantee of rights in manually supplied assets.

### Grow the fact pool

```sh
# Continues each recipe's saved cursor; does not repeat earlier pages.
python3 -m triviadb import wikidata --recipes all --pages 2

# More familiar subjects, using sitelink count as a rough popularity proxy.
python3 -m triviadb import wikidata --recipes film-director,book-author,album-artist --min-sitelinks 60

# Known entity IDs can avoid SPARQL discovery.
python3 -m triviadb import wikidata --recipes book-author --ids Q208460,Q25338

# Use an already-downloaded official Met file.
python3 -m triviadb import met --file /path/to/MetObjects.csv
```

Discovery is deliberately polite and bounded. Do **not** use thousands of pages to mine Wikidata's public query service. For large-scale extraction, use an appropriately sized local subset or the [official JSON dumps](https://www.wikidata.org/wiki/Wikidata:Database_download):

```sh
python3 -m triviadb import wikidata-dump /path/to/entities.json.gz --max-records 1000000
# Rerun that exact command to continue.
```

Supported formats: official one-entity-per-line JSON arrays or JSONL, optionally gzip/bzip2. Two streaming passes retain eligible subjects and their referenced English labels; a final extraction pass creates facts. It never loads the complete dump into memory. Staging data stays in SQLite. **Compressed-file resumes re-decompress the prefix**, so a late restart can take time. A full Wikidata dump can require hundreds of GB or more, including staging; it is never downloaded automatically and is not the recommended laptop starting point.

Snapshots are pinned for reproducibility, not automatically refreshed. Source disagreements discovered during subsequent imports quarantine the conflicting facts and unpublish affected questions. Cross-source identity matching uses Wikidata IDs when available; unmatched local IDs are not magically resolved.

## Quality, difficulty, and variety

1. Import structured facts, source record IDs, URLs, licenses, source snapshots, and supporting context.
2. Deduplicate **before** model calls: canonical subject + predicate + answer + semantic qualifiers. A subject/predicate/scope slot also catches conflicting answers.
3. Reject multi-answer, unknown-value, deprecated-only, and unsupported qualified Wikidata statements. Met imports reject uncertain/multiple artist attributions and generic anonymous artifacts.
4. Choose answers deterministically from distinct peers in the same relationship pool, preferring similar eras/prominence. Calculated geography retains every coordinate operand and avoids near-ties.
5. Gemini edits the question and optional supported notes. Code fixes category, subcategory, all four choices, and media identity.
6. A **separate model request** reviews source consistency, one-correct-answer status, ambiguity, distractor plausibility, timeliness, notes, answer leakage, US-adult relevance, and pub-worthiness. It inspects/listens to attachments when present. Every review check must be a boolean `true` to publish.
7. A final SQLite uniqueness gate checks normalized text, plus the stimulus for media/calculated comparisons. Rejected drafts and reasons remain available for inspection.

**This is not a proof of correctness, an independent web fact-check, or empirical difficulty calibration.** Sources can be wrong; model reviewers can agree on mistakes. Famous works, broadly recognizable facts, and manual sampling matter more than raw quantity. A Met “highlight” is not necessarily familiar to a pub audience. Initial testing caught overly obscure questions, implausible alternatives, optimistic difficulty estimates, and photo clues that supplied the artwork title. Manual corrections and stronger prompts were applied; a local validation rule now also rejects media clues containing their full work title. These checks reduce errors, but do not replace sampling or player calibration.

### Inspect small batches before scaling

```sh
python3 -m triviadb generate --limit 30 --max-calls 20
python3 -m triviadb sample --limit 30 --seed 42 > data/pilot.json
python3 -m triviadb stats

# Easier sample for a general audience:
python3 -m triviadb sample --limit 30 --max-difficulty 6 --seed 42

# Inspect sources, draft, and review; ID can be a question UUID or fact fingerprint.
python3 -m triviadb inspect QUESTION_UUID

# Editorial correction: keep the question but adjust its estimated difficulty.
python3 -m triviadb set-difficulty QUESTION_UUID 7 --reason "Requires specialist recall; revisit after playtesting"

# Or unpublish without discarding the evidence.
python3 -m triviadb reject QUESTION_UUID --reason "Too obscure for our audience"
```

`sample` shuffles reproducibly, balances available categories, and targets **50% easy (1–3), 40% medium (4–6), 10% hard (7–9)**. If coverage is insufficient it uses available questions; it does not invent questions or silently change their difficulty. Supply the printed seed to repeat a sample. The output includes correct answers for editorial inspection, not blind gameplay.

Generation itself interleaves categories, prioritizes prominent subjects, and breaks ties using hashed fact IDs—not alphabetical question titles. Wikidata pagination is by entity ID, not title. After partial imports, unvisited/least-recently-visited recipes run first so repeated quota stops cannot continually favor the front of the recipe list. Early small imports can still be unrepresentative: import multiple recipes/pages and inspect `stats` before evaluating corpus coverage. Met and Natural Earth are sampled from their downloaded datasets, not only the first alphabetical records.

The database need not contain the same difficulty mix as a playable party set. Preserve good harder questions, but select an accessible mix for games. `set-difficulty` preserves the original model review alongside the manual estimate. Actual player correct-answer rates are the next calibration step; this version does **not** collect player responses. No empirical quality claims are made from the model's difficulty score.

Tuning knobs already available:
- `--min-sitelinks` and `--recipes`: source prominence and topic coverage.
- `--category`, `--type`, `--limit`, `--batch-size`: bounded targeted generation/review.
- `sample --min-difficulty/--max-difficulty`: party-set selection.
- `EDITOR` and `REVIEWER` in `triviadb/pipeline.py`: editorial standards.
- `RECIPES` in `triviadb/catalog.py`: defined relationships and reusable subcategories.

Prompt changes affect future writing/review; they do not silently rewrite published or rejected candidates. Generation limits count attempted candidates, including drafts/rejections and resumed reviews—not net database growth.

## Photo and sound questions

```sh
# Downloads only explicitly designated Met Open Access images.
python3 -m triviadb media met --limit 10
python3 -m triviadb generate --type photo --limit 10 --max-calls 20
python3 -m triviadb media list
```

The Met image importer requires `isPublicDomain=true`, an official image host, and stored API/license evidence. It associates the image with an unprocessed fact. A photo question must require the image, not merely decorate an otherwise answerable text question.

For other images or recordings, **you must establish asset-level rights**:

```sh
python3 -m triviadb media import /path/to/recording.wav \
  --type sound --license CC0-1.0 --rights-confirmed \
  --source-url 'https://example.org/the-recording' \
  --license-url 'https://example.org/the-recording/license' \
  --creator 'Recording creator' \
  --evidence 'This specific recording is explicitly released under CC0; underlying content rights are cleared.' \
  --start 0 --seconds 10 --fact-id FACT_FINGERPRINT

# Or attach an already-imported asset to an unprocessed fact:
python3 -m triviadb media attach --fact-id FACT_FINGERPRINT --media-id MEDIA_UUID
python3 -m triviadb generate --type sound --limit 5 --max-calls 10
```

Use real source/license URLs in place of the examples. The fact must already exist and the media must genuinely support it. Manual audio import is **not automatic audio discovery**, nor automated verification of your license assertion. The model review must also find the attachment appropriate and consistent with the fact.

- MusicBrainz's CC0 **metadata does not license music recordings**.
- Short clips are not automatically fair use. Both a composition and a recording can carry rights.
- Allowed policy: CC0/public domain only. CC BY/NC/SA assets are not currently accepted.
- Photos become metadata-stripped JPEGs with maximum dimensions of 1600×1600.
- Sound becomes metadata-stripped mono 44.1 kHz, 16-bit WAV, maximum 30 seconds. Choose a clip within the source duration.
- Filenames are content hashes, never artwork/artist/answer names. On-image text or spoken answers still need review.
- License records, creator, source, evidence, transformations, and attribution are preserved separately.

### Laptop storage and S3

The default media-library cap is **2 GiB**, configurable with `--max-media-mb`. The cap covers registered sanitized assets, **not** downloaded source snapshots, temporary files, SQLite staging, or exported copies.

```sh
python3 -m triviadb --max-media-mb 1024 media met --limit 20
python3 -m triviadb --media-dir /Volumes/External/trivia-media media met --limit 20
```

Images often take tens to hundreds of KB; a 10-second WAV is approximately **0.88 MB**. `stats` reports actual media/cache bytes and free disk space. The initial source snapshot download is roughly 350 MB. Keep an eye on export copies and do not download a full Wikidata dump onto a nearly full laptop.

S3 is not required and no uploader is implemented. Exported assets use relative paths and question IDs reference stable media UUIDs. Your game can later serve the exported media folder from S3/CDN without changing question IDs. Building/reviewing media currently requires local files; no S3 credentials are needed.

## Gemini keys, quotas, and restarts

`.env` accepts a comma-delimited list:

```dotenv
GEMINI_API_KEY="key-one,key-two,key-three"
GEMINI_WRITER_MODEL="gemini-3.5-flash-lite"
GEMINI_REVIEWER_MODEL="gemini-3.5-flash"
# Optional project labels aligned with the keys, so shared quotas cool down together:
GEMINI_KEY_PROJECTS="project-a,project-a,project-b"
```

Keys are deduplicated, rotated least-recently-used, and never printed or stored in SQLite. Only hashes and scheduling state are persisted. There is a global per-model request interval across keys (`--interval`, default 6 seconds). Retry-After/RetryInfo and daily quota resets are honored. Authentication failures disable that key/model; transient failures cool down; permanent request/model errors stop instead of cycling credentials.

**Quotas apply per Google project, not per key.** Without project labels, cooldowns are per key and may encounter the same shared quota more than once. Twenty-one keys do not necessarily mean twenty-one quotas. No billing is enabled by this app; nevertheless the API cannot prove your keys are free-tier, so check AI Studio. Request budgets bound attempts, not monetary spending or tokens.

```sh
python3 -m triviadb models
python3 -m triviadb keys
python3 -m triviadb generate --limit 100 --max-calls 40
python3 -m triviadb generate --draft-only --limit 20 --max-calls 10
python3 -m triviadb review --limit 20 --max-calls 10
```

The defaults were live-tested. Model availability changes; a listed model may still reject new users. Choose another available model with CLI flags or `.env`. After correcting credential configuration, `keys --reset` clears local disabled/cooldown records; it does **not** reset Google's quotas.

Ctrl-C is safe: rerun the same import/generate/review command. Completed source batches, drafts, reviews, API responses, and cooldowns persist. Valid identical model requests are reused. A crash between a remote success and the local save can still repeat **that one request**; no client can guarantee exactly-once remote billing without provider idempotency. Final facts/questions remain unique.

A partial download is never treated as complete; incomplete downloads restart rather than combining different source versions. One writer per database is enforced with an OS file lock, automatically released after a crash. Exit codes: `0` success, `1` error, `75` safely paused (quota/budget/lock), `130` interrupted.

## Database and export

`questions` contains only approved rows:

| Field | Meaning |
|---|---|
| `id` | Generated UUIDv4 |
| `category`, `subcategory` | Defined major category and reusable topic |
| `question` | Standalone clue/question |
| `a` | Correct answer |
| `b`, `c`, `d` | Plausible incorrect answers |
| `difficulty` | Editorial integer estimate, 1–9 |
| `notes` | Optional supported post-answer fact/aside |
| `type` | `text`, `photo`, or `sound` |
| `media_id` | NULL for text; required linked asset for photo/sound |

Supporting tables include `facts`, `provenance`, `candidates`, `question_meta`, `media`, `fact_media`, `checkpoints`, `jobs`, and hashed `key_state`/`api_attempts`.

```sh
python3 -m triviadb export data/party.sqlite
```

Exports approved questions, associated facts/provenance, and required media to **`data/party.sqlite`** and **`data/party.media/`**. It checks integrity/checksums and refuses to overwrite existing destinations. Builder queues, model responses, and key state are not exported. Retain the working database and source cache for the full review/snapshot audit trail.

Game integration:
- **Shuffle `a`–`d` for every play** while retaining which choice is correct. Never always display `a` first.
- A photo/sound question requires its asset. Resolve exported media paths relative to the exported SQLite file.
- Source URLs, attribution, creator, filenames from the original source, and notes can reveal answers: display them after answering, not as pre-answer captions.
- Use accessible image labels/audio controls; provide text-only rounds where visual/audio questions would exclude players.

## Bounded development pilot

The saved pilot contains **717 deduplicated source facts** and **16 retained questions across nine categories** (15 text, one photo), from 30 attempted candidates. Fourteen were rejected by the model or subsequent editorial inspection. The retained difficulty estimates are nine easy, five medium, and two hard; two initial model estimates were manually raised. This is a small quality probe, **not** a claim of a finished large corpus or player-tested calibration.

- Preview: `data/pilot.json` (seed 42).
- Approved-only export: `data/party.sqlite` plus `data/party.media/`.
- Working SQLite: about 1.41 MiB; source cache: about 390 MiB; six stored photos: about 474 KiB combined. Only the retained photo is copied into the export.
- Text/photo Gemini calls ran successfully. Audio conversion/attachment/export was tested offline with a synthetic WAV, not a live licensed-audio corpus.
- Wikidata rate-limited the broader discovery run; completed recipes/facts were preserved. More discovery can resume later, prioritizing untouched recipes.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

Offline tests use temporary SQLite databases and fake HTTP transports, never your keys. They cover identity/conflicts, strict reviews, resume/rollback, source ambiguity, distinct distractors, pagination past ineligible facts, quota persistence, malformed response retries, media rights/clipping/caps, export integrity/assets, manual rejection, and balanced seeded sampling. The audio-processing test skips if ffmpeg/ffprobe are unavailable.
