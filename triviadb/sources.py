"""Source adapters: licensed structured data, never existing trivia wording."""

import bz2
import csv
import gzip
import json
import math
import re
import time
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlencode

from .catalog import RECIPES
from .store import add_fact, checkpoint, digest, dumps, entity_id, normalize, unpack

MET_CSV = "https://media.githubusercontent.com/media/metmuseum/openaccess/master/MetObjects.csv"
EARTH_JSON = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_10m_populated_places.geojson"
WD_API = "https://www.wikidata.org/w/api.php"


def label(entity):
    return entity.get("labels", {}).get("en", {}).get("value", "")


def claims(entity, prop):
    return [c for c in entity.get("claims", {}).get(prop, []) if c.get("rank") != "deprecated"]


def claim_value(claim):
    snak = claim.get("mainsnak", {})
    if snak.get("snaktype") != "value":
        return None
    return snak.get("datavalue", {})


def reference_ids(entity):
    ids = set()
    for prop in {row[1] for row in RECIPES.values()}:
        for claim in claims(entity, prop):
            value = claim_value(claim) or {}
            if value.get("type") == "wikibase-entityid":
                ids.add(value["value"]["id"])
    return ids


def matching_recipes(entity):
    classes = {(claim_value(c) or {}).get("value", {}).get("id") for c in claims(entity, "P31")}
    return [key for key, row in RECIPES.items() if row[0] in classes]


def extract_wikidata(entity, recipe, labels, snapshot):
    _, prop, category, subcategory, _ = RECIPES[recipe]
    active = claims(entity, prop)
    if not label(entity) or not active or any(c.get("qualifiers") or not claim_value(c) for c in active):
        return None  # Fail closed on unknown answers or unhandled statement scopes.
    values = {dumps(claim_value(c)): claim_value(c) for c in active}
    if len(values) != 1:
        return None
    value = next(iter(values.values()))
    if value["type"] == "wikibase-entityid":
        oid = value["value"]["id"]
        answer = label(labels.get(oid, {}))
        oid = entity_id(oid)
    elif prop == "P1086" and value["type"] == "quantity":
        raw = value["value"]
        number = float(raw["amount"])
        if raw.get("unit", "1") != "1" or not number.is_integer() or not 1 <= number <= 118:
            return None
        answer, oid = str(int(number)), "atomic-number:" + str(int(number))
    elif prop == "P246" and value["type"] == "string":
        answer = value["value"]
        if not re.fullmatch(r"[A-Z][a-z]?", answer):
            return None
        oid = "chemical-symbol:" + answer
    else:
        return None
    if not answer or len(answer) > 160:
        return None
    description = entity.get("descriptions", {}).get("en", {}).get("value", "")
    context = {"description": description, "references": active[0].get("references", []),
               "statement_ids": [c.get("id") for c in active], "source_revision": entity.get("lastrevid")}
    # Descriptions are CC0 entity metadata, not Wikipedia prose. A year is disambiguation, not a new fact.
    year = re.match(r"^(\d{4})\b", description)
    if year:
        context["year"] = int(year[1])
    return dict(subject_id=entity_id(entity["id"]), subject=label(entity), predicate=prop,
                object_id=oid, answer=answer, category=category, subcategory=subcategory, pool=recipe,
                qualifiers={}, context=context, popularity=entity.get("_sitelink_count", len(entity.get("sitelinks", {}))),
                source="wikidata", source_record_id=entity["id"],
                source_url="https://www.wikidata.org/wiki/" + entity["id"], license="CC0-1.0", snapshot=snapshot)


def get_entities(http, ids):
    entities, paths = {}, []
    ordered = sorted(set(ids))
    for start in range(0, len(ordered), 50):
        url = WD_API + "?" + urlencode(dict(action="wbgetentities", ids="|".join(ordered[start:start + 50]),
                                            props="labels|descriptions|claims|sitelinks", languages="en", format="json", maxlag=5))
        result, path = http.json(url)
        if "entities" not in result:
            Path(path).unlink(missing_ok=True)
            raise ValueError("Wikidata entity API temporarily unavailable or returned an error; rerun to resume")
        entities.update(result["entities"])
        paths.append(path)
    return entities, paths


def import_wikidata(db, http, recipes, pages=1, page_size=50, min_sitelinks=25, ids=None):
    states = {r: checkpoint(db, "wikidata:" + digest([r, min_sitelinks, sorted(ids) if ids else None])) for r in recipes}
    # A quota stop must not permanently favor the first recipes on every restart.
    for recipe in sorted(states, key=lambda r: (bool(states[r]), states[r].get("visited", 0))):
        if recipe not in RECIPES:
            raise ValueError(f"Unknown recipe: {recipe}")
        key = "wikidata:" + digest([recipe, min_sitelinks, sorted(ids) if ids else None])
        for _ in range(1 if ids else pages):
            state = checkpoint(db, key)
            if state.get("done"):
                break
            if ids:
                found = ids
            else:
                kind, prop = RECIPES[recipe][:2]
                after = state.get("after", "")
                if after and not re.fullmatch(r"http://www\.wikidata\.org/entity/Q\d+", after):
                    raise ValueError("Invalid Wikidata checkpoint")
                query = f'''PREFIX wd: <http://www.wikidata.org/entity/>
PREFIX wdt: <http://www.wikidata.org/prop/direct/>
PREFIX wikibase: <http://wikiba.se/ontology#>
PREFIX schema: <http://schema.org/>
SELECT DISTINCT ?item WHERE {{
 ?item wdt:P31 wd:{kind}; wdt:{prop} ?answer; wikibase:sitelinks ?links.
 ?article schema:about ?item; schema:isPartOf <https://en.wikipedia.org/>.
 FILTER(?links >= {int(min_sitelinks)}) FILTER(STR(?item) > "{after}")
}} ORDER BY ?item LIMIT {int(page_size)}'''
                result, _ = http.json("https://query.wikidata.org/sparql?" + urlencode({"query": query, "format": "json"}), interval=2)
                found = [r["item"]["value"].rsplit("/", 1)[-1] for r in result["results"]["bindings"]]
            if not found:
                with db:
                    checkpoint(db, key, {**state, "done": True, "visited": time.time()})
                break
            subjects, snapshots = get_entities(http, found)
            needed = set().union(*(reference_ids(e) for e in subjects.values()))
            objects, object_snapshots = get_entities(http, needed)
            count = 0
            with db:
                for entity in subjects.values():
                    if recipe not in matching_recipes(entity) or "enwiki" not in entity.get("sitelinks", {}):
                        continue
                    data = extract_wikidata(entity, recipe, objects, dumps(snapshots + object_snapshots))
                    if data:
                        add_fact(db, data)
                        count += 1
                checkpoint(db, key, {"after": "http://www.wikidata.org/entity/" + found[-1],
                                     "done": bool(ids) or len(found) < page_size, "visited": time.time()})
            print(f"Wikidata {recipe}: {len(found)} records, {count} usable facts (existing facts deduplicated)", flush=True)


def extract_met(row, snapshot):
    artist = (row.get("Artist Display Name") or "").strip()
    title = (row.get("Title") or "").strip()
    if row.get("Is Highlight", "").lower() != "true" or not artist or not title:
        return None
    if (row.get("Artist Prefix", "").strip() or row.get("Artist Suffix", "").strip()
            or "|" in artist or re.search(r"\b(unknown|anonymous|workshop|school of|circle of|after|attributed)\b", artist, re.I)):
        return None
    classification = row.get("Classification", "")
    if classification not in ("Paintings", "Sculpture", "Drawings", "Prints"):
        return None  # Avoid turning anonymous vessels and fragmentary catalog entries into trivia.
    oid = row.get("Object Wikidata URL") or "met:" + row["Object ID"]
    aid = row.get("Artist Wikidata URL") or "met-artist:" + (row.get("Artist ULAN URL") or normalize(artist))
    if "|" in oid or "|" in aid:
        return None
    year = row.get("Object Begin Date", "")
    context = {"description": f"{classification} in The Metropolitan Museum of Art collection",
               "date": row.get("Object Date", ""), "medium": row.get("Medium", ""),
               "artist_bio": row.get("Artist Display Bio", ""), "met_object_id": row["Object ID"]}
    if re.fullmatch(r"\d{4}", year):
        context["year"] = int(year)
    pool = "painting-artist" if classification == "Paintings" else "sculpture-artist" if classification == "Sculpture" else "met-works-on-paper"
    subcategory = "Paintings & Artists" if classification == "Paintings" else "Sculpture" if classification == "Sculpture" else "Drawings & Prints"
    return dict(subject_id=oid, subject=title, predicate="P170", object_id=aid, answer=artist,
                category="Arts & Culture", subcategory=subcategory, pool=pool, popularity=60,
                qualifiers={}, context=context, source="met", source_record_id=row["Object ID"],
                source_url="https://www.metmuseum.org/art/collection/search/" + row["Object ID"],
                license="CC0-1.0", snapshot=snapshot)


def file_key(path):
    stat = Path(path).stat()
    return digest([str(Path(path).resolve()), stat.st_size, stat.st_mtime_ns])


def import_met(db, http, path=None, max_records=None):
    path = Path(path) if path else http.get(MET_CSV, suffix=".csv", max_bytes=1_000_000_000)
    key = "met:" + file_key(path)
    state = checkpoint(db, key)
    if state.get("done"):
        print("Met snapshot already imported.")
        return
    start, count, buffer = state.get("row", 0), 0, []
    with path.open(encoding="utf-8-sig", newline="") as file:
        rows = csv.DictReader(file)
        if not {"Object ID", "Title", "Artist Display Name", "Is Highlight"}.issubset(rows.fieldnames or []):
            raise ValueError("Not a Met CSV (if this is a Git LFS pointer, download from media.githubusercontent.com)")
        for index, row in enumerate(rows, 1):
            if index <= start:
                continue
            buffer.append((index, extract_met(row, str(path))))
            if len(buffer) >= 1000 or (max_records and index - start >= max_records):
                with db:
                    for _, data in buffer:
                        if data:
                            add_fact(db, data)
                            count += 1
                    checkpoint(db, key, {"row": index})
                buffer.clear()
                if max_records and index - start >= max_records:
                    print(f"Met: checkpoint row {index}, {count} usable facts", flush=True)
                    return
        with db:
            for _, data in buffer:
                if data:
                    add_fact(db, data)
                    count += 1
            checkpoint(db, key, {"done": True})
    print(f"Met: {count} usable highlighted works imported (existing facts deduplicated)", flush=True)


def natural_facts(collection, snapshot):
    groups = defaultdict(dict)
    for feature in collection["features"]:
        p = feature["properties"]
        name, qid = p.get("NAME_EN") or p.get("NAMEASCII"), p.get("WIKIDATAID")
        latitude = p.get("LATITUDE")
        us = p.get("ADM0_A3") == "USA"
        if not name or not qid or type(latitude) not in (int, float) or not math.isfinite(latitude) or not -90 <= latitude <= 90:
            continue
        if (p.get("POP_MAX") or 0) < (100000 if us else 1000000):
            continue  # Population is only a recognition filter, never a trivia claim.
        group = "U.S. Cities" if us else "World Cities"
        place = p.get("ADM1NAME") if us else p.get("ADM0NAME")
        display = f"{name}, {place}" if place else name
        groups[group][qid] = dict(id=entity_id(qid), name=display, latitude=latitude)
    for group in sorted(groups):
        cities = sorted(groups[group].values(), key=lambda c: digest(c["id"]))
        for start in range(0, len(cities) - 3, 4):
            choices = cities[start:start + 4]
            ranked = sorted(choices, key=lambda c: c["latitude"], reverse=True)
            if ranked[0]["latitude"] - ranked[1]["latitude"] < 1 or len({normalize(c["name"]) for c in choices}) != 4:
                continue  # Clear margin; coordinates represent city points, not municipal boundaries.
            winner = ranked[0]
            members = sorted(c["id"] for c in choices)
            yield dict(subject_id="city-group:" + digest(members), subject="these four cities",
                       predicate="northernmost", object_id=winner["id"], answer=winner["name"],
                       category="Geography", subcategory=group, pool="city-latitude", popularity=50,
                       qualifiers={"members": members}, context={"cities": choices, "rule": "maximum latitude of city center"},
                       fixed_options=[winner["name"], *(c["name"] for c in choices if c != winner)],
                       source="natural-earth", source_record_id=digest(members),
                       source_url="https://www.naturalearthdata.com/downloads/10m-cultural-vectors/10m-populated-places/",
                       license="public-domain", snapshot=snapshot)


def import_natural(db, http):
    path = http.get(EARTH_JSON, max_bytes=60_000_000)
    collection = json.loads(path.read_text(encoding="utf-8"))
    count = 0
    with db:
        for data in natural_facts(collection, str(path)):
            add_fact(db, data)
            count += 1
        checkpoint(db, "natural-earth:" + file_key(path), {"done": True})
    print(f"Natural Earth: {count} calculated facts (existing facts deduplicated)", flush=True)


def choose_options(db, fact):
    if fact.get("fixed_options"):
        return fact["fixed_options"]
    # ponytail: compare 512 distinct prominent peers; widen this if alias-heavy pools need more coverage.
    rows = db.execute("""SELECT object_id,answer,context,popularity FROM (
        SELECT object_id,answer,context,popularity,
            row_number() OVER (PARTITION BY object_id ORDER BY popularity DESC,id) AS position
        FROM facts WHERE pool=? AND status='ready' AND object_id<>?
        ) WHERE position=1 ORDER BY popularity DESC,object_id LIMIT 512""",
                      (fact["pool"], fact["object_id"])).fetchall()
    year = fact["context"].get("year")
    def distance(row):
        other_year = json.loads(row["context"]).get("year")
        era = abs(year - other_year) // 15 if year and other_year else 100
        return era, abs(fact["popularity"] - row["popularity"]), digest([fact["id"], row["object_id"]])
    options, seen_ids = [fact["answer"]], {fact["object_id"]}
    seen_names = {normalize(fact["answer"])}
    for row in sorted(rows, key=distance):
        name = normalize(row["answer"])
        if row["object_id"] in seen_ids or not name or name in seen_names:
            continue
        options.append(row["answer"])
        seen_ids.add(row["object_id"])
        seen_names.add(name)
        if len(options) == 4:
            return options
    return None


def iter_dump(path, after=0):
    opener = gzip.open if str(path).endswith(".gz") else bz2.open if str(path).endswith(".bz2") else open
    with opener(path, "rt", encoding="utf-8") as stream:
        for line_no, line in enumerate(stream, 1):
            if line_no <= after:
                continue
            line = line.strip().rstrip(",")
            if line in ("", "[", "]"):
                continue
            try:
                yield line_no, json.loads(line)
            except ValueError:
                raise ValueError(f"Invalid entity JSON on dump line {line_no}; expected one entity per line") from None


def import_dump(db, path, min_sitelinks=25, max_records=None):
    """Two streaming passes: retain selected subjects, then only their needed English labels."""
    path = Path(path)
    key = "dump:" + digest([file_key(path), min_sitelinks])
    db.executescript("""
        CREATE TABLE IF NOT EXISTS dump_subjects (import_key TEXT,id TEXT,payload TEXT,PRIMARY KEY(import_key,id));
        CREATE TABLE IF NOT EXISTS dump_labels (import_key TEXT,id TEXT,payload TEXT,PRIMARY KEY(import_key,id));
    """)
    state = checkpoint(db, key) or {"phase": "subjects", "line": 0}
    if state["phase"] == "done":
        print("Wikidata dump already imported.")
        return
    scanned = 0
    for phase in ("subjects", "labels"):
        if state["phase"] != phase:
            continue
        needed = None
        if phase == "labels":
            needed = set()
            for row in db.execute("SELECT payload FROM dump_subjects WHERE import_key=?", (key,)):
                needed.update(reference_ids(json.loads(row[0])))
        buffer, line_no = [], state.get("line", 0)
        for line_no, entity in iter_dump(path, line_no):
            scanned += 1
            if phase == "subjects":
                if "enwiki" in entity.get("sitelinks", {}) and len(entity["sitelinks"]) >= min_sitelinks and matching_recipes(entity):
                    entity["_sitelink_count"] = len(entity["sitelinks"])
                    entity["sitelinks"] = {"enwiki": entity["sitelinks"]["enwiki"]}
                    buffer.append((key, entity["id"], dumps(entity)))
            elif entity.get("id") in needed and label(entity):
                buffer.append((key, entity["id"], dumps({"labels": {"en": {"value": label(entity)}}})))
            if scanned % 1000 == 0 or (max_records and scanned >= max_records):
                with db:
                    db.executemany(f"INSERT OR IGNORE INTO dump_{phase} VALUES (?,?,?)", buffer)
                    checkpoint(db, key, {"phase": phase, "line": line_no})
                buffer.clear()
                if scanned % 100000 == 0:
                    print(f"Dump {phase}: line {line_no:,}", flush=True)
                if max_records and scanned >= max_records:
                    print(f"Dump {phase}: paused at line {line_no:,}; rerun the same command to resume", flush=True)
                    return
        state = {"phase": "labels" if phase == "subjects" else "extract", "line": 0}
        with db:
            db.executemany(f"INSERT OR IGNORE INTO dump_{phase} VALUES (?,?,?)", buffer)
            checkpoint(db, key, state)
    if state["phase"] == "extract":
        after, count = state.get("after", ""), 0
        while True:
            rows = db.execute("SELECT id,payload FROM dump_subjects WHERE import_key=? AND id>? ORDER BY id LIMIT 500", (key, after)).fetchall()
            if not rows:
                break
            with db:
                for row in rows:
                    entity = json.loads(row["payload"])
                    labels = {}
                    for qid in reference_ids(entity):
                        value = db.execute("SELECT payload FROM dump_labels WHERE import_key=? AND id=?", (key, qid)).fetchone()
                        if value:
                            labels[qid] = json.loads(value[0])
                    for recipe in matching_recipes(entity):
                        data = extract_wikidata(entity, recipe, labels, str(path))
                        if data:
                            add_fact(db, data)
                            count += 1
                after = rows[-1]["id"]
                checkpoint(db, key, {"phase": "extract", "after": after})
        with db:
            checkpoint(db, key, {"phase": "done"})
        print(f"Wikidata dump: {count} usable facts extracted (existing facts deduplicated)", flush=True)
