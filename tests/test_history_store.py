import base64
import importlib.util
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from dealradar.core import History
from dealradar.hunter import HuntMemory
from test_hunter import NOW, deal

spec = importlib.util.spec_from_file_location("history_store", pathlib.Path(__file__).parents[1]/"scripts/history_store.py")
store = importlib.util.module_from_spec(spec)
spec.loader.exec_module(store)


class FakeGitHub:
    def __init__(self, raw=None):
        self.raw, self.calls = raw, []

    def call(self, path, method="GET", data=None, *, missing=False):
        self.calls.append((path, method, data))
        if path.startswith("/git/ref/heads/"):
            return {"object": {"sha": "state"}} if self.raw else None
        if method == "POST":
            return {"ref": "refs/heads/radar-state"}
        if method == "PUT":
            self.raw = base64.b64decode(data["content"])
            return {"content": {"sha": "new"}}
        if self.raw:
            return {"encoding": "base64", "content": base64.b64encode(self.raw).decode(), "sha": "old"}
        if missing:
            return None
        raise RuntimeError("State missing")


def database(path, when=NOW):
    history = History(path)
    memory = HuntMemory(history, "channel")
    memory.confirm([deal()], when, "digest", 1)
    history.close()
    return path.read_bytes()


class HistoryStoreTests(unittest.TestCase):
    def test_receipts_restore_after_cache_loss(self):
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            raw = database(base/"source.sqlite3")
            target = base/"data"/"history.sqlite3"
            store.restore(FakeGitHub(raw), target)
            history = History(target)
            self.assertFalse(HuntMemory(history, "channel").digest_due(NOW, 12))
            history.close()

    def test_newer_cache_is_preserved_after_durable_save_failure(self):
        import datetime as dt
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            raw = database(base/"old.sqlite3")
            target = base/"history.sqlite3"
            database(target, NOW+dt.timedelta(hours=1))
            before = target.read_bytes()
            store.restore(FakeGitHub(raw), target)
            self.assertEqual(target.read_bytes(), before)

    def test_corrupt_remote_state_cannot_replace_local_receipts(self):
        with tempfile.TemporaryDirectory() as directory:
            target = pathlib.Path(directory)/"history.sqlite3"
            before = database(target)
            with self.assertRaises(ValueError):
                store.restore(FakeGitHub(b"not SQLite"), target)
            self.assertEqual(target.read_bytes(), before)

    def test_save_uses_existing_file_sha_and_dedicated_branch(self):
        with tempfile.TemporaryDirectory() as directory:
            target = pathlib.Path(directory)/"history.sqlite3"
            api = FakeGitHub(database(target))
            store.save(api, target)
            path, method, payload = api.calls[-1]
            self.assertEqual((path, method), ("/contents/history.sqlite3", "PUT"))
            self.assertEqual(payload["sha"], "old")
            self.assertEqual(payload["branch"], "radar-state")
            self.assertNotIn("secret", str(payload))

    def test_initial_save_bootstraps_branch_without_overwriting_a_file(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", GITHUB_SHA="base-sha"):
            target = pathlib.Path(directory)/"history.sqlite3"
            database(target)
            api = FakeGitHub()
            store.save(api, target)
            self.assertIn(("/git/refs", "POST", {"ref": "refs/heads/radar-state", "sha": "base-sha"}), api.calls)
            self.assertNotIn("sha", api.calls[-1][2])

    def test_existing_branch_with_unreadable_file_stops_restore(self):
        api = FakeGitHub(b"state")
        original = api.call
        def call(path, *args, **kwargs):
            if path.startswith("/contents/"):
                raise RuntimeError("Access denied")
            return original(path, *args, **kwargs)
        api.call = call
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(RuntimeError):
            store.restore(api, pathlib.Path(directory)/"history.sqlite3")


if __name__ == "__main__":
    unittest.main()
