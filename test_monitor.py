import contextlib
import copy
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import monitor


class FakeAPI:
    def __init__(self):
        self.issues = [{"number": 1, "title": "Test", "body": "original", "state": "open",
                        "html_url": "https://github.com/test/repo/issues/1"},
                       {"number": 2, "pull_request": {}, "title": "PR"}]
        self.comments = []
        self.sha = "a" * 40
        self.fail = False
        self.status = "ahead"
        self.files = [{"filename": "app.js", "status": "modified"}]

    def pages(self, path, params=None, key=None):
        if path.startswith("/commits/"):
            if self.fail:
                raise RuntimeError("file request failed")
            yield copy.deepcopy(self.files)
            return
        yield copy.deepcopy(self.issues if path == "/issues" else self.comments)

    def get(self, path, params=None):
        if self.fail:
            raise RuntimeError("network unavailable")
        if not path:
            return {"default_branch": "main"}, {}
        if path.startswith("/compare/"):
            return {"status": self.status, "commits": [self.commit()], "html_url": "https://example.com/compare"}, {}
        return self.commit(), {}

    def commit(self):
        return {"sha": self.sha, "html_url": "https://example.com/commit/" + self.sha,
                "commit": {"message": "old dated commit\n\nbody", "committer": {"date": "2001-01-01T00:00:00Z"}}}


class MonitorTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = {"repo": "test/repo", "data_dir": str(self.root), "branches": []}
        self.api = FakeAPI()
        self.tick()

    def tearDown(self):
        self.temp.cleanup()

    def tick(self, notify="none"):
        with contextlib.redirect_stdout(io.StringIO()):
            monitor.check(self.config, notify=notify, api=self.api)

    def batches(self):
        return [json.loads(p.read_text(encoding="utf-8")) for p in (self.root / "raw").glob("*.json")]

    def test_baseline_and_no_change_silent(self):
        self.tick()
        self.assertEqual([], self.batches())
        with contextlib.closing(sqlite3.connect(self.root / "state.sqlite")) as db:
            self.assertEqual(1, db.execute("SELECT count(*) FROM snapshots WHERE kind='issue'").fetchone()[0])

    def test_issue_closed_comment_and_dedup(self):
        self.api.issues[0]["state"] = "closed"
        self.api.comments = [{"id": 10, "issue_url": "https://api.github.com/repos/test/repo/issues/1",
                              "body": "hello", "updated_at": "2026-10-26T00:00:00Z",
                              "user": {"login": "tester"}, "html_url": "https://example.com/comment"},
                             {"id": 11, "issue_url": "https://api.github.com/repos/test/repo/issues/2"}]
        self.tick()
        self.tick()
        self.assertEqual(1, len(self.batches()))
        self.assertEqual(["issue", "comment"], [e["kind"] for e in self.batches()[0]["events"]])

    def test_old_dated_commit(self):
        self.api.sha = "b" * 40
        self.tick()
        self.tick()
        self.assertEqual(1, len(self.batches()))
        self.assertEqual("commit", self.batches()[0]["events"][0]["kind"])

    def test_force_push(self):
        self.api.sha = "c" * 40
        self.api.status = "diverged"
        self.tick()
        self.assertEqual(["branch", "commit"], [e["kind"] for e in self.batches()[0]["events"]])

    def test_failure_rolls_back_snapshots(self):
        self.api.issues[0]["title"] = "updated before API fails"
        self.api.fail = True
        with self.assertRaises(RuntimeError):
            self.tick()
        self.assertEqual([], self.batches())
        self.api.fail = False
        self.tick()
        self.assertEqual(1, len(self.batches()[0]["events"]))

    def test_notification_retry(self):
        self.api.sha = "d" * 40
        with patch.object(monitor, "terminal_notify", side_effect=OSError("open failed")):
            with self.assertRaises(OSError):
                self.tick("terminal")
        with patch.object(monitor, "terminal_notify") as notify:
            self.tick("terminal")
            self.tick("terminal")
            self.assertEqual(1, notify.call_count)
        self.assertEqual(1, len(self.batches()))

    def test_untrusted_text_stays_in_data(self):
        self.api.issues[0]["title"] = "$(touch /tmp/DO_NOT_CREATE) `id` \x1b[31m"
        self.tick()
        report = next((self.root / "reports").glob("*.md"))
        events = self.batches()[0]["events"]
        with patch.object(monitor.sys, "platform", "darwin"), patch.object(monitor.subprocess, "run") as run:
            monitor.terminal_notify(report, events, self.root / "notifications")
            self.assertEqual(["/usr/bin/open", "-a", "Terminal"], run.call_args.args[0][:3])
        command = next((self.root / "notifications").glob("*.command")).read_text(encoding="utf-8")
        self.assertNotIn("touch", command)
        self.assertNotIn("\x1b", report.read_text(encoding="utf-8"))

    def test_pagination(self):
        api = monitor.GitHub("test/repo")
        with patch.object(api, "get", side_effect=[([1], {"Link": '<url>; rel="next"'}), ([2], {})]):
            self.assertEqual([[1], [2]], list(api.pages("/issues")))

    def test_path_filter_match_and_report(self):
        self.config["include_paths"] = ["app.js", "assets/"]
        self.api.sha = "e" * 40
        self.tick()
        event = self.batches()[0]["events"][0]
        self.assertEqual([{"filename": "app.js", "status": "modified"}], event["matched_files"])
        self.assertIn("app.js (modified)", next((self.root / "reports").glob("*.md")).read_text(encoding="utf-8"))

    def test_unmatched_commit_advances_head_and_keeps_issue(self):
        self.config["include_paths"] = ["assets/"]
        self.api.sha = "e" * 40
        self.api.issues[0]["state"] = "closed"
        self.tick()
        self.tick()
        self.assertEqual(["issue"], [e["kind"] for e in self.batches()[0]["events"]])
        with contextlib.closing(sqlite3.connect(self.root / "state.sqlite")) as db:
            self.assertEqual(self.api.sha, monitor.meta(db, "head:main"))
            self.assertTrue(db.execute("SELECT 1 FROM commits WHERE sha=?", (self.api.sha,)).fetchone())

    def test_directory_recursive_and_boundary(self):
        self.config["include_paths"] = ["assets/", "app.js"]
        self.api.sha = "f" * 40
        self.api.files = [{"filename": path, "status": "modified"} for path in
                          ("assets/a.svg", "assets/sub/b.png", "assets-old/a.svg", "sub/app.js", "app.jsx")]
        self.tick()
        files = self.batches()[0]["events"][0]["matched_files"]
        self.assertEqual(["assets/a.svg", "assets/sub/b.png"], [f["filename"] for f in files])

    def test_rename_both_directions_and_delete(self):
        self.config["include_paths"] = ["assets/"]
        self.api.sha = "f" * 40
        self.api.files = [{"filename": "old/a.png", "previous_filename": "assets/a.png", "status": "renamed"},
                          {"filename": "assets/b.png", "previous_filename": "old/b.png", "status": "renamed"},
                          {"filename": "assets/c.png", "status": "removed"}]
        self.tick()
        self.assertEqual(3, len(self.batches()[0]["events"][0]["matched_files"]))

    def test_unmatched_only_no_report(self):
        self.config["include_paths"] = ["assets/"]
        self.api.sha = "f" * 40
        self.tick()
        self.assertEqual([], self.batches())

    def test_file_list_failure_retries_without_losing_commit(self):
        self.config["include_paths"] = ["app.js"]
        self.api.sha = "f" * 40
        original_pages = self.api.pages
        def failing(path, params=None, key=None):
            if path.startswith("/commits/"):
                raise RuntimeError("file list unavailable")
            yield from original_pages(path, params, key)
        with patch.object(self.api, "pages", side_effect=failing):
            with self.assertRaises(RuntimeError):
                self.tick()
        self.tick()
        self.assertEqual(1, len(self.batches()[0]["events"]))

    def test_path_validation(self):
        for invalid in ("app.js", ["/app.js"], ["../app.js"], ["assets//"], ["*.js"], [""]):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    monitor.include_paths({"include_paths": invalid})

    def test_file_list_pagination(self):
        api = monitor.GitHub("test/repo")
        with patch.object(api, "get", side_effect=[({"files": [{"filename": "README.md"}]},
                                                   {"Link": '<url>; rel="next"'}),
                                                  ({"files": [{"filename": "assets/a.png", "status": "added"}]}, {})]):
            files, matched = monitor.matching_files(monitor.GitHubSource(api), "a" * 40, ["assets/"])
        self.assertEqual(2, len(files))
        self.assertEqual("assets/a.png", matched[0]["filename"])


if __name__ == "__main__":
    unittest.main()
