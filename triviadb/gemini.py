"""Gemini REST, bounded retries, persisted cooldowns, and durable response caching."""

import base64
import hashlib
import json
import math
import os
import re
import shlex
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .net import HTTPFailure, Paused, request_json, retry_after_seconds
from .store import checkpoint, digest, dumps

BASE = "https://generativelanguage.googleapis.com/v1beta"


def load_config(env_path=".env"):
    values = {}
    path = Path(env_path)
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            match = re.match(r"^\s*(?:export\s+)?([A-Z_]+)\s*=\s*(.*)$", line)
            if match:
                values[match[1]] = " ".join(shlex.split(match[2], comments=True))
    values.update({key: value for key, value in os.environ.items() if key.startswith("GEMINI_")})
    raw_keys = [k.strip() for k in values.get("GEMINI_API_KEY", "").split(",") if k.strip()]
    projects = [p.strip() for p in values.get("GEMINI_KEY_PROJECTS", "").split(",") if p.strip()]
    if projects and len(projects) != len(raw_keys):
        raise ValueError("GEMINI_KEY_PROJECTS must have one project label per configured key")
    unique = dict(zip(raw_keys, projects or [None] * len(raw_keys)))
    return list(unique), list(unique.values()), values


def validate_schema(value, schema):
    """Validate the small JSON Schema subset used by our own request schemas."""
    types = schema.get("type")
    types = [types] if isinstance(types, str) else types
    matches = {"object": type(value) is dict, "array": type(value) is list, "string": type(value) is str,
               "integer": type(value) is int, "boolean": type(value) is bool, "null": value is None,
               "number": type(value) in (int, float) and math.isfinite(value)}
    if types and not any(matches.get(kind, False) for kind in types):
        raise ValueError("Model output violates the requested JSON type")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError("Model output contains an unexpected enum value")
    if isinstance(value, dict):
        if not set(schema.get("required", [])).issubset(value):
            raise ValueError("Model output is missing required fields")
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False and not set(value).issubset(props):
            raise ValueError("Model output contains unrequested fields")
        for key in value.keys() & props.keys():
            validate_schema(value[key], props[key])
    elif isinstance(value, list):
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", math.inf):
            raise ValueError("Model output returned the wrong number of items")
        for item in value:
            validate_schema(item, schema.get("items", {}))
    elif isinstance(value, str):
        if not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", math.inf):
            raise ValueError("Model output contains a string of invalid length")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            raise ValueError("Model output contains blank or invalid text")
    elif type(value) in (int, float):
        if not schema.get("minimum", -math.inf) <= value <= schema.get("maximum", math.inf):
            raise ValueError("Model output contains an out-of-range number")


def retry_until(error, now):
    seconds = retry_after_seconds(error.retry_after, now)
    raw = dumps(error.body).lower()
    for delay in re.findall(r'"retrydelay":"([\d.]+)s"', raw):
        seconds = max(seconds, float(delay))
    if re.search(r"perday|per_day|per day|requests/day", raw):
        local = datetime.fromtimestamp(now, ZoneInfo("America/Los_Angeles"))
        reset = (local + timedelta(days=1)).replace(hour=0, minute=0, second=5, microsecond=0)
        return max(now + seconds, reset.timestamp())
    return now + seconds


class Gemini:
    def __init__(self, db, keys, projects=None, *, interval=6, max_calls=100, transport=request_json):
        self.db, self.keys = db, list(dict.fromkeys(keys))
        self.transport, self.interval, self.max_calls, self.calls = transport, interval, max_calls, 0
        self.projects = dict(zip(keys, projects or [None] * len(keys)))
        if not self.keys:
            raise ValueError("No Gemini keys; set GEMINI_API_KEY in .env or the environment")
        db.execute("""CREATE TABLE IF NOT EXISTS api_attempts (
            id INTEGER PRIMARY KEY, stage TEXT, model TEXT, fingerprint TEXT,
            code INTEGER, response TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
        db.commit()

    def redact(self, text):
        for key in self.keys:
            text = text.replace(key, "[REDACTED]")
        return text

    def call(self, stage, model, payload=None):
        if not re.fullmatch(r"[A-Za-z0-9._-]+", model):
            raise ValueError("Invalid Gemini model name")
        with self.db:
            for key in self.keys:
                fingerprint = digest(key)
                project = digest(self.projects.get(key) or fingerprint)
                self.db.execute("""INSERT INTO key_state(fingerprint,model,project) VALUES (?,?,?)
                    ON CONFLICT(fingerprint,model) DO UPDATE SET project=excluded.project""", (fingerprint, model, project))
        while True:
            if self.calls >= self.max_calls:
                raise Paused("Request budget reached. Rerun the same command to resume.")
            available = []
            for key in self.keys:
                row = self.db.execute("SELECT * FROM key_state WHERE fingerprint=? AND model=?", (digest(key), model)).fetchone()
                if not row["disabled"] and row["next_at"] <= time.time():
                    available.append((row["last_used"], self.keys.index(key), key, row))
            if not available:
                raise Paused("All configured keys are disabled or cooling down for this model. Use `keys` to inspect; rerun later.")
            _, _, key, state = min(available)
            throttle = "gemini-throttle:" + model
            delay = checkpoint(self.db, throttle).get("next_at", 0) - time.time()
            if delay > 0:
                time.sleep(delay)
            now = time.time()
            with self.db:
                checkpoint(self.db, throttle, {"next_at": now + self.interval})
                self.db.execute("UPDATE key_state SET last_used=? WHERE fingerprint=? AND model=?", (now, digest(key), model))
                attempt = self.db.execute("INSERT INTO api_attempts(stage,model,fingerprint) VALUES (?,?,?)", (stage, model, digest(key))).lastrowid
            self.calls += 1
            try:
                url = BASE + ("/models" if stage == "models" else f"/models/{model}:generateContent")
                response = self.transport(url, data=payload, headers={"x-goog-api-key": key})
            except HTTPFailure as exc:
                permanent = False
                with self.db:
                    self.db.execute("UPDATE api_attempts SET code=?,response=? WHERE id=?", (exc.code, self.redact(dumps(exc.body)), attempt))
                    invalid_key = exc.code in (401, 403) or (exc.code == 400 and re.search(r"api.key.*(?:invalid|not valid)|API_KEY_INVALID", dumps(exc.body), re.I))
                    if invalid_key:
                        self.db.execute("UPDATE key_state SET disabled=1 WHERE fingerprint=? AND model=?", (digest(key), model))
                    elif exc.code == 429:
                        self.db.execute("UPDATE key_state SET next_at=? WHERE model=? AND project=?", (retry_until(exc, now), model, state["project"]))
                    elif exc.code == 0 or 500 <= exc.code < 600:
                        self.db.execute("""UPDATE key_state SET next_at=?,failures=failures+1
                            WHERE fingerprint=? AND model=?""", (now + min(300, 2 ** min(state["failures"] + 2, 8)), digest(key), model))
                    else:
                        permanent = True
                if permanent:
                    message = self.redact(str(exc.body.get("error", {}).get("message", "Check model/request configuration")))[:500]
                    raise RuntimeError(f"Gemini HTTP {exc.code}: {message}") from None
                continue
            with self.db:
                self.db.execute("UPDATE api_attempts SET code=200,response=? WHERE id=?", (self.redact(dumps(response)), attempt))
                self.db.execute("UPDATE key_state SET failures=0 WHERE fingerprint=? AND model=?", (digest(key), model))
            return response

    def complete(self, stage, model, prompt, schema, attachments=()):
        job = digest(["v1", stage, model, prompt, schema, [a["sha256"] for a in attachments]])
        cached = self.db.execute("SELECT response FROM jobs WHERE id=?", (job,)).fetchone()
        if cached:
            try:
                parsed = json.loads(cached[0])["data"]
                validate_schema(parsed, schema)
                return parsed
            except (ValueError, KeyError, TypeError):
                with self.db:
                    self.db.execute("DELETE FROM jobs WHERE id=?", (job,))
        parts = [{"text": prompt}]
        for asset in attachments:
            raw = Path(asset["path"]).read_bytes()
            if len(raw) > 10_000_000 or hashlib.sha256(raw).hexdigest() != asset["sha256"]:
                raise ValueError("Media is oversized or has changed since import")
            parts += [{"text": "Attachment for fact_id " + asset["fact_id"]},
                      {"inlineData": {"mimeType": asset["mime_type"], "data": base64.b64encode(raw).decode()}}]
        config = {"responseMimeType": "application/json", "responseJsonSchema": schema,
                  "temperature": 0.8 if stage == "write" else 0.1, "maxOutputTokens": 8192}
        if model.startswith("gemini-2.5"):
            config["thinkingConfig"] = {"thinkingBudget": 0 if stage == "write" else 1024}
        response = self.call(stage, model, {"contents": [{"role": "user", "parts": parts}], "generationConfig": config})
        candidates = response.get("candidates", [])
        if not candidates or candidates[0].get("finishReason") != "STOP":
            raise ValueError("Gemini blocked or truncated its response; no questions published. Reduce batch size and retry.")
        text = "".join(p.get("text", "") for p in candidates[0].get("content", {}).get("parts", []) if not p.get("thought"))
        try:
            parsed = json.loads(text)
        except ValueError:
            raise ValueError("Gemini returned invalid JSON; no questions published. Rerun to retry.") from None
        validate_schema(parsed, schema)
        expected = schema.get("properties", {}).get("items", {}).get("items", {}).get("properties", {}).get("fact_id", {}).get("enum")
        if expected is not None and sorted(item["fact_id"] for item in parsed["items"]) != sorted(expected):
            raise ValueError("Gemini omitted or duplicated fact IDs; no questions published. Rerun to retry.")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO jobs VALUES (?,?,?,?,CURRENT_TIMESTAMP)",
                            (job, stage, model, dumps({"data": parsed, "usage": response.get("usageMetadata", {})})))
        return parsed
