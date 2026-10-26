import contextlib
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch, MagicMock

import monitor
import runtime
import sources


class GitLabFixture:
    def __init__(self):
        self.sha = "a" * 40
        self.issue = {"iid": 7, "title": "内网工单", "description": "正文", "state": "opened",
                      "labels": ["backend"], "assignees": [{"username": "dev"}],
                      "web_url": "https://gitlab.internal/team/repo/-/issues/7"}
        self.notes = []
        self.diffs = [{"new_path": "watched/a.txt", "old_path": "watched/a.txt", "new_file": True}]
        self.timeout = False

    def commit(self):
        return {"id": self.sha, "message": "内网提交\n正文", "web_url": "https://gitlab.internal/commit/" + self.sha}

    def get(self, path, params=None):
        if not path:
            return {"default_branch": "main", "web_url": "https://gitlab.internal/team/repo"}, {}
        if path.startswith("/repository/commits/"):
            return self.commit(), {}
        if path == "/repository/compare":
            return {"commits": [self.commit()] if params["to"] == self.sha else [],
                    "compare_timeout": self.timeout}, {}
        raise AssertionError(path)

    def pages(self, path, params=None, key=None):
        if path == "/issues":
            yield [copy.deepcopy(self.issue)]
        elif path == "/issues/7/notes":
            yield copy.deepcopy(self.notes)
        elif path.endswith("/diff"):
            yield copy.deepcopy(self.diffs)
        else:
            raise AssertionError(path)


class MigrationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = {"provider": "gitlab", "repo": "team/sub/repo",
                       "api_base_url": "https://gitlab.internal/api/v4",
                       "data_dir": str(self.root), "gitlab_diff_max_files": 1000,
                       "include_paths": ["watched/"]}
        self.client = GitLabFixture()

    def tearDown(self):
        self.temp.cleanup()

    def source(self):
        return sources.GitLabSource(self.config, self.client)

    def tick(self):
        with contextlib.redirect_stdout(io.StringIO()):
            monitor.check(self.config, "file", source=self.source())

    def test_gitlab_full_pipeline_and_dedup(self):
        self.tick()
        self.client.issue["state"] = "closed"
        self.client.notes = [{"id": 11, "body": "新评论", "updated_at": monitor.now(),
                              "created_at": monitor.now(), "author": {"username": "dev"}, "system": False},
                             {"id": 12, "created_at": monitor.now(), "system": True}]
        self.client.sha = "b" * 40
        self.tick()
        self.tick()
        raw = list((self.root / "raw").glob("*.json"))
        self.assertEqual(1, len(raw))
        events = json.loads(raw[0].read_text(encoding="utf-8"))["events"]
        self.assertEqual(["issue", "comment", "commit"], [e["kind"] for e in events])
        self.assertEqual("closed", events[0]["after"]["state"])
        self.assertEqual("watched/a.txt", events[2]["matched_files"][0]["filename"])

    def test_gitlab_rename_old_path(self):
        self.client.diffs = [{"old_path": "watched/a", "new_path": "elsewhere/a", "renamed_file": True}]
        files, matched = monitor.matching_files(self.source(), self.client.sha, ["watched/"])
        self.assertEqual("watched/a", matched[0]["previous_filename"])

    def test_gitlab_missing_or_reached_file_limit(self):
        self.config["gitlab_diff_max_files"] = None
        with self.assertRaises(ValueError):
            self.source().files(self.client.sha)
        self.config["gitlab_diff_max_files"] = 1
        with self.assertRaises(RuntimeError):
            self.source().files(self.client.sha)

    def test_gitlab_compare_timeout(self):
        self.client.timeout = True
        with self.assertRaises(RuntimeError):
            self.source().compare("a", "b")

    def test_source_change_does_not_reuse_state(self):
        self.tick()
        self.config["api_base_url"] = "https://other.internal/api/v4"
        with self.assertRaises(ValueError):
            self.tick()

    def test_old_github_state_migrates_identity(self):
        db = monitor.connect(self.root / "legacy.sqlite")
        try:
            with db:
                monitor.setmeta(db, "initialized", "1")
                monitor.setmeta(db, "repo", "team/repo")
                from test_monitor import FakeAPI
                monitor.collect(db, sources.GitHubSource(FakeAPI()), {"repo": "team/repo"}, monitor.now())
            self.assertIsNotNone(monitor.meta(db, "source_identity"))
        finally:
            db.close()

    def test_gitlab_project_nested_path_and_header(self):
        config = {**self.config, "auth": {"token_env": "TEST_INTERNAL_TOKEN", "use_gh": False}}
        with patch.dict(os.environ, {"TEST_INTERNAL_TOKEN": "test-placeholder"}):
            client = sources.Transport(config, "/projects/" + sources.urllib.parse.quote(config["repo"], safe=""))
        response = MagicMock()
        response.read.return_value = b'{}'
        response.headers = {}
        response.__enter__.return_value = response
        with patch.object(client.opener, "open", return_value=response) as opened:
            client.get("/issues")
        request = opened.call_args.args[0]
        self.assertIn("team%2Fsub%2Frepo", request.full_url)
        self.assertEqual("test-placeholder", request.get_header("Private-token"))

    def test_enterprise_gh_credential_host(self):
        config = {"provider": "github", "api_base_url": "https://github.internal/api/v3"}
        with patch.dict(os.environ, {"GH_TOKEN": "must-not-use-public-token"}, clear=True), \
             patch.object(sources.shutil, "which", return_value="gh"), \
             patch.object(sources.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "enterprise-token")) as run:
            self.assertEqual("enterprise-token", sources.token(config))
            self.assertEqual(["gh", "auth", "token", "--hostname", "github.internal"], run.call_args.args[0])

    def test_token_file_and_relative_paths(self):
        (self.root / "secret.txt").write_text("fixture-token\n", encoding="utf-8")
        config = {**self.config, "auth": {"token_file": "secret.txt", "use_gh": False},
                  "network": {"ca_file": "company.pem"}}
        file = self.root / "config.json"
        file.write_text(json.dumps(config), encoding="utf-8")
        loaded = monitor.load_config(file)
        self.assertEqual(str((self.root / "company.pem").resolve()), loaded["network"]["ca_file"])
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual("fixture-token", sources.token(loaded))

    def test_gitlab_pagination_header(self):
        with patch.object(sources, "token", return_value=None):
            client = sources.Transport(self.config, "/projects/1")
        with patch.object(client, "get", side_effect=[([1], {"X-Next-Page": "2"}), ([2], {})]):
            self.assertEqual([[1], [2]], list(client.pages("/issues")))

    def test_windows_console_argv(self):
        with patch.object(runtime, "console_python", return_value=r"C:\Python\python.exe"), \
             patch.object(runtime.subprocess, "Popen") as popen:
            runtime.windows_notify(r"C:\Program Files\monitor.py", r"C:\data\a% &中文.txt")
        args = popen.call_args.args[0]
        self.assertEqual(r"C:\data\a% &中文.txt", args[-2])
        self.assertEqual("--pause", args[-1])
        self.assertNotIn("cmd.exe", args)
        self.assertEqual(0x10, popen.call_args.kwargs["creationflags"])

    def test_windows_scheduler(self):
        with patch.object(runtime, "windows_task_command", return_value='"C:\\Python\\pythonw.exe" command'), \
             patch.object(runtime.subprocess, "run") as run, contextlib.redirect_stdout(io.StringIO()):
            runtime.install_windows(self.root / "config.json", self.config)
        args = run.call_args.args[0]
        self.assertIn("/IT", args)
        self.assertEqual("5", args[args.index("/MO") + 1])

    def test_windows_lock_branch(self):
        fake = MagicMock()
        with patch.dict("sys.modules", {"msvcrt": fake}), patch.object(runtime.os, "name", "nt"):
            with runtime.process_lock(self.root / "win.lock") as acquired:
                self.assertTrue(acquired)
        self.assertEqual(2, fake.locking.call_count)

    def test_second_process_lock_is_skipped(self):
        with runtime.process_lock(self.root / "lock") as first:
            with runtime.process_lock(self.root / "lock") as second:
                self.assertTrue(first)
                self.assertFalse(second)

    def test_redirect_refuses_to_forward_credentials(self):
        with self.assertRaises(RuntimeError):
            sources.NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.invalid")

    def test_scheduled_config_error_is_logged(self):
        log = self.root / "logs/error.log"
        with patch("sys.argv", ["monitor.py", "--config", str(self.root / "missing.json"),
                                "--log-file", str(log), "check"]):
            self.assertEqual(1, monitor.main())
        self.assertIn("检查失败", log.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
