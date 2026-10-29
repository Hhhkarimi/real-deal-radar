"""Persist SQLite receipts on a dedicated branch, independent of cache eviction."""
import base64
import datetime as dt
import json
import os
import pathlib
import re
import sqlite3
import sys
import urllib.error
import urllib.request
from contextlib import closing

BRANCH = "radar-state"
FILE = "history.sqlite3"


class GitHub:
    def __init__(self):
        repository = os.environ["GITHUB_REPOSITORY"]
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("Invalid repository")
        self.base = "https://api.github.com/repos/" + repository
        self.token = os.environ["GH_TOKEN"]

    def call(self, path, method="GET", data=None, *, missing=False):
        request = urllib.request.Request(self.base + path, method=method,
            data=json.dumps(data).encode() if data is not None else None,
            headers={"Authorization": "Bearer " + self.token, "Accept": "application/vnd.github+json",
                     "Content-Type": "application/json", "User-Agent": "real-deal-radar"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if missing and error.code == 404:
                return None
            raise RuntimeError(f"History storage HTTP {error.code}") from None


def validate(path):
    with closing(sqlite3.connect(path)) as db:
        if db.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise ValueError("History database integrity check failed")


def restore(api, path):
    ref = api.call("/git/ref/heads/" + BRANCH, missing=True)
    if ref is None:
        # The existing cache is used only to migrate the old posting memory.
        if path.exists():
            validate(path)
        print("History: initializing durable state from existing cache or first run")
        return
    item = api.call(f"/contents/{FILE}?ref={BRANCH}")
    if item.get("encoding") == "none" and item.get("sha"):
        item = api.call("/git/blobs/" + item["sha"])
    if item.get("encoding") != "base64":
        raise ValueError("Unsupported history encoding")
    raw = base64.b64decode(item["content"].replace("\n", ""), validate=True)
    if not raw.startswith(b"SQLite format 3\x00"):
        raise ValueError("History is not a SQLite database")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".restore.sqlite3")
    temporary.write_bytes(raw)
    validate(temporary)
    if path.exists():
        validate(path)
        if latest_event(path) > latest_event(temporary):
            temporary.unlink()
            print("History: kept newer cached memory after an interrupted durable save")
            return
    temporary.replace(path)
    print("History: restored durable delivery and scan memory")


def save(api, path):
    if not path.exists():
        print("History: no database to save")
        return
    validate(path)
    ref = api.call("/git/ref/heads/" + BRANCH, missing=True)
    if ref is None:
        base = os.environ["GITHUB_SHA"]
        api.call("/git/refs", "POST", dict(ref="refs/heads/" + BRANCH, sha=base))
    previous = api.call(f"/contents/{FILE}?ref={BRANCH}", missing=ref is None)
    payload = dict(message="Persist deal hunting memory [skip ci]", branch=BRANCH,
                   content=base64.b64encode(path.read_bytes()).decode())
    if previous:
        payload["sha"] = previous["sha"]
    # The contents SHA makes conflicting updates fail rather than overwrite.
    api.call("/contents/" + FILE, "PUT", payload)
    print("History: saved durable delivery and scan memory")


def latest_event(path):
    latest = 0
    with closing(sqlite3.connect(path)) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table, column in [("hunt_checks", "checked"), ("hunt_attempts", "attempted_at"),
                              ("hunt_deliveries", "sent_at"), ("channel_posts", "sent_at"), ("prices", "seen")]:
            if table in tables:
                value = db.execute(f"SELECT MAX({column}) FROM {table}").fetchone()[0]
                if value:
                    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
                    if parsed.tzinfo is None:
                        raise ValueError("History timestamps require a timezone")
                    latest = max(latest, parsed.timestamp())
    return latest


def main():
    try:
        api = GitHub()
        operation = {"restore": restore, "save": save}[sys.argv[1]]
        operation(api, pathlib.Path("data/history.sqlite3"))
    except Exception:
        raise SystemExit("History persistence failed; publishing must remain stopped until state is restored") from None


if __name__ == "__main__":
    main()
