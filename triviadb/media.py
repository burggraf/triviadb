"""Optional, rights-checked media. ffmpeg strips metadata and bounds asset size."""

import hashlib
import json
import math
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from .store import checkpoint, dumps

MET_LICENSE = "https://www.metmuseum.org/about-the-met/policies-and-documents/open-access"


def http_url(value):
    return isinstance(value, str) and urlsplit(value).scheme in ("http", "https") and bool(urlsplit(value).netloc)


def run_media(command):
    try:
        return subprocess.run(command, capture_output=True, check=True, timeout=90).stdout
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        raise ValueError("Media conversion/probe failed; check the file and ffmpeg installation") from None


def import_asset(db, source, directory, *, kind, license_id, rights_confirmed, source_url, license_url,
                 creator, evidence, start=0, seconds=10, max_bytes=2 * 1024**3):
    if license_id not in ("CC0-1.0", "public-domain") or rights_confirmed is not True:
        raise ValueError("Explicit CC0/public-domain asset rights confirmation is required (including recording rights)")
    if not http_url(source_url) or not http_url(license_url) or not creator.strip() or not evidence.strip():
        raise ValueError("Source URL, license URL, creator, and asset-level license evidence are required")
    if kind not in ("photo", "sound"):
        raise ValueError("Media kind must be photo or sound")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise ValueError("Media needs ffmpeg and ffprobe. On macOS: brew install ffmpeg")
    if not all(type(v) in (int, float) and math.isfinite(v) for v in (start, seconds)) or start < 0 or not 0 < seconds <= 30:
        raise ValueError("Clip start must be nonnegative and duration between 0 and 30 seconds")
    source, directory = Path(source).resolve(), Path(directory).resolve()
    if source.stat().st_size > 100_000_000:
        raise ValueError("Source asset exceeds the 100 MB input limit")
    directory.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(directory).free < 200_000_000:
        raise ValueError("Less than 200 MB free; move the media directory or free disk space")
    probe = json.loads(run_media(["ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe", "-show_streams",
                                  "-show_format", "-of", "json", str(source)]))
    streams = [s for s in probe.get("streams", []) if s.get("codec_type") == ("video" if kind == "photo" else "audio")]
    if not streams:
        raise ValueError("Input has no matching image/audio stream")
    if kind == "photo" and streams[0].get("width", 0) * streams[0].get("height", 0) > 100_000_000:
        raise ValueError("Image exceeds the 100-megapixel decoding limit")
    if kind == "sound":
        duration = float(probe.get("format", {}).get("duration", 0))
        if start + seconds > duration + 0.02:
            raise ValueError(f"Clip extends beyond the {duration:.2f}-second recording; shorten --seconds")
    suffix, mime = (".jpg", "image/jpeg") if kind == "photo" else (".wav", "audio/wav")
    with tempfile.TemporaryDirectory(prefix=".convert-", dir=directory) as temp:
        output = Path(temp) / ("asset" + suffix)
        command = ["ffmpeg", "-v", "error", "-nostdin", "-protocol_whitelist", "file,pipe", "-i", str(source)]
        if kind == "photo":
            command += ["-map", "0:v:0", "-frames:v", "1", "-vf", "scale=min(1600\\,iw):min(1600\\,ih):force_original_aspect_ratio=decrease", "-q:v", "3"]
        else:
            command += ["-ss", str(start), "-t", str(seconds), "-map", "0:a:0", "-vn", "-c:a", "pcm_s16le", "-ar", "44100", "-ac", "1"]
        run_media(command + ["-map_metadata", "-1", "-fflags", "+bitexact", "-flags:a", "+bitexact", str(output)])
        raw = output.read_bytes()
        sha = hashlib.sha256(raw).hexdigest()
        existing = db.execute("SELECT id FROM media WHERE sha256=?", (sha,)).fetchone()
        if existing:
            return existing[0]
        used = db.execute("SELECT coalesce(sum(bytes),0) FROM media").fetchone()[0]
        if used + len(raw) > max_bytes:
            raise ValueError("Media storage cap reached; raise --max-media-mb or move storage")
        target = directory / (sha + suffix)
        output.replace(target)
        mid = str(uuid.uuid4())
        with db:
            db.execute("""INSERT INTO media
                (id,kind,path,sha256,bytes,mime_type,source_url,creator,license,license_url,evidence,
                 attribution,alt_text,start_seconds,duration_seconds) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (mid, kind, str(target), sha, len(raw), mime, source_url, creator, license_id, license_url,
                        evidence, f"{creator}; {license_id}; {source_url}. Resized/converted or excerpted; no endorsement implied.",
                        "Artwork for this question." if kind == "photo" else "Listen to the audio clip for this question.",
                        start if kind == "sound" else None, seconds if kind == "sound" else None))
    return mid


def attach(db, fact_id, media_id):
    if db.execute("SELECT 1 FROM candidates WHERE fact_id=?", (fact_id,)).fetchone():
        raise ValueError("Attach media before drafting; existing questions are not silently changed")
    if not db.execute("SELECT 1 FROM facts WHERE id=? AND status='ready'", (fact_id,)).fetchone():
        raise ValueError("Fact does not exist or is quarantined")
    if not db.execute("SELECT 1 FROM media WHERE id=?", (media_id,)).fetchone():
        raise ValueError("Media does not exist")
    with db:
        db.execute("INSERT INTO fact_media VALUES (?,?) ON CONFLICT(fact_id) DO UPDATE SET media_id=excluded.media_id", (fact_id, media_id))


def import_met_media(db, http, directory, limit=10, max_bytes=2 * 1024**3):
    rows = db.execute("""SELECT DISTINCT f.id,p.source_record_id FROM facts f JOIN provenance p ON p.fact_id=f.id
        WHERE p.source='met' AND f.status='ready'
        AND NOT EXISTS(SELECT 1 FROM candidates c WHERE c.fact_id=f.id)
        AND NOT EXISTS(SELECT 1 FROM fact_media m WHERE m.fact_id=f.id) ORDER BY f.popularity DESC,f.id""").fetchall()
    count = 0
    for row in rows:
        if count >= limit:
            break
        key = "met-media:" + row["source_record_id"]
        if checkpoint(db, key).get("unavailable"):
            continue
        url = "https://collectionapi.metmuseum.org/public/collection/v1/objects/" + row["source_record_id"]
        item, snapshot = http.json(url)
        image_url = item.get("primaryImageSmall") or item.get("primaryImage")
        if item.get("isPublicDomain") is not True or not image_url or urlsplit(image_url).hostname != "images.metmuseum.org":
            with db:
                checkpoint(db, key, {"unavailable": True})
            continue
        image = http.get(image_url, suffix=".image", max_bytes=100_000_000)
        mid = import_asset(db, image, directory, kind="photo", license_id="CC0-1.0", rights_confirmed=True,
                           source_url=item.get("objectURL") or url, license_url=MET_LICENSE,
                           creator=item.get("artistDisplayName") or "The Metropolitan Museum of Art",
                           evidence=dumps({"objectID": item["objectID"], "isPublicDomain": True,
                                           "image_url": image_url, "api_url": url, "api_snapshot": snapshot}), max_bytes=max_bytes)
        attach(db, row["id"], mid)
        count += 1
        print(f"Met Open Access: attached photo {count}/{limit}", flush=True)
    print(f"Attached {count} Met photos; unverified/missing images were skipped.", flush=True)
