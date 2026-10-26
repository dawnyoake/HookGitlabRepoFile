#!/usr/bin/env python3
"""Independent GitHub/GitLab monitor for macOS/Windows; Python standard library only."""
import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import urllib.error

from sources import GitHub, GitHubSource, create_source, source_identity, api_base
from runtime import process_lock, windows_notify, install_windows, uninstall_windows


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def clean(value):
    return "".join(c for c in str(value or "") if c in "\n\t" or
                   (ord(c) >= 32 and not 127 <= ord(c) <= 159))


def connect(path):
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS snapshots
          (kind TEXT, id TEXT, value TEXT NOT NULL, PRIMARY KEY(kind,id));
        CREATE TABLE IF NOT EXISTS commits (sha TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS batches
          (id TEXT PRIMARY KEY, report TEXT NOT NULL, notified INTEGER NOT NULL DEFAULT 0);
    """)
    return db


def meta(db, key, default=None):
    row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def setmeta(db, key, value):
    db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, value))


def put_snapshot(db, kind, ident, value):
    row = db.execute("SELECT value FROM snapshots WHERE kind=? AND id=?", (kind, str(ident))).fetchone()
    old = json.loads(row[0]) if row else None
    db.execute("INSERT OR REPLACE INTO snapshots VALUES (?,?,?)",
               (kind, str(ident), json.dumps(value, ensure_ascii=False, sort_keys=True)))
    return old


def issue_snapshot(issue):
    return {"number": issue["number"], "title": issue["title"], "body": issue.get("body") or "",
            "state": issue["state"], "state_reason": issue.get("state_reason"),
            "labels": sorted(x["name"] for x in issue.get("labels", [])),
            "assignees": sorted(x["login"] for x in issue.get("assignees", [])),
            "milestone": (issue.get("milestone") or {}).get("number")}


def include_paths(config):
    paths = config.get("include_paths", [])
    if not isinstance(paths, list):
        raise ValueError("include_paths 必须是路径字符串数组")
    for path in paths:
        if (not isinstance(path, str) or not path or path.startswith("/") or
                "\\" in path or "//" in path or any(c in path for c in "*?[]") or
                any(part in ("", ".", "..") for part in path.rstrip("/").split("/"))):
            raise ValueError("include_paths 使用仓库相对路径，目录以 / 结尾，不支持通配符")
    return paths


def matches_path(filename, paths):
    return any(filename.startswith(path) if path.endswith("/") else filename == path for path in paths)


def matching_files(api, sha, paths):
    files = api.files(sha)
    matched = [file for file in files if matches_path(file["filename"], paths) or
               (file.get("previous_filename") and matches_path(file["previous_filename"], paths))]
    return files, [{key: file[key] for key in ("filename", "previous_filename", "status") if key in file}
                   for file in matched]


def collect(db, api, config, start):
    paths = include_paths(config)
    baseline = meta(db, "initialized") is None
    previous_repo = meta(db, "repo")
    if previous_repo and previous_repo != str(config["repo"]):
        raise ValueError("数据目录已经用于另一仓库，请使用新的 data_dir")
    last = meta(db, "checked_at")
    since = None
    if last:
        since = (dt.datetime.fromisoformat(last.replace("Z", "+00:00")) -
                 dt.timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    identity = source_identity(config)
    stored_identity = meta(db, "source_identity")
    if stored_identity and stored_identity != identity:
        raise ValueError("数据目录属于不同平台、API 地址或仓库，请为内网部署使用新的 data_dir")
    if not stored_identity and not baseline and (config.get("provider", "github") != "github" or
                                                api_base(config) != "https://api.github.com"):
        raise ValueError("旧数据目录来自 GitHub，请为新平台使用新的 data_dir")
    events, raw = [], {"issues": [], "comments": [], "branches": {}}
    for issue in api.issues(since):
        raw["issues"].append(issue)
        snapshot = issue_snapshot(issue)
        old = put_snapshot(db, "issue", issue["number"], snapshot)
        if not baseline and old != snapshot:
            events.append({"kind": "issue", "action": "新增" if old is None else "更新",
                           "title": f'#{issue["number"]} {issue["title"]}',
                           "url": issue["html_url"], "before": old, "after": snapshot,
                           "body": snapshot["body"]})
    known_issues = {row[0] for row in db.execute("SELECT id FROM snapshots WHERE kind='issue'")}
    for comment in api.comments(since):
        number = comment["issue_url"].rsplit("/", 1)[-1]
        if number not in known_issues:
            continue
        raw["comments"].append(comment)
        snapshot = {"body": comment.get("body") or "", "updated_at": comment["updated_at"]}
        old = put_snapshot(db, "comment", comment["id"], snapshot)
        if not baseline and old != snapshot:
            events.append({"kind": "comment", "action": "新增" if old is None else "编辑",
                           "title": f'Issue #{number} · {comment["user"]["login"]}',
                           "url": comment["html_url"], "body": snapshot["body"],
                           "before": old, "after": snapshot})
    branches = config.get("branches") or [api.default_branch()]
    for branch in branches:
        head = api.head(branch)
        old_head = meta(db, "head:" + branch)
        raw["branches"][branch] = {"head": head}
        if old_head and old_head != head["sha"]:
            comparison = api.compare(old_head, head["sha"])
            commits = comparison["commits"]
            raw["branches"][branch]["comparison"] = {**comparison, "commits": commits}
            if comparison["status"] in ("behind", "diverged"):
                events.append({"kind": "branch", "action": "历史变化", "title": branch,
                               "url": comparison.get("html_url", head["html_url"]),
                               "body": f'{old_head} → {head["sha"]} ({comparison["status"]})'})
            for commit in commits:
                seen = db.execute("SELECT 1 FROM commits WHERE sha=?", (commit["sha"],)).fetchone()
                db.execute("INSERT OR IGNORE INTO commits VALUES (?)", (commit["sha"],))
                if not baseline and not seen:
                    matched = []
                    if paths:
                        files, matched = matching_files(api, commit["sha"], paths)
                        raw.setdefault("commit_files", {})[commit["sha"]] = files
                        if not matched:
                            continue
                    event = {"kind": "commit", "action": "新增", "branch": branch,
                                   "title": commit["sha"][:8] + " " + commit["commit"]["message"].splitlines()[0],
                                   "url": commit["html_url"], "body": commit["commit"]["message"]}
                    if paths:
                        event["matched_files"] = matched
                    events.append(event)
        db.execute("INSERT OR IGNORE INTO commits VALUES (?)", (head["sha"],))
        setmeta(db, "head:" + branch, head["sha"])
    setmeta(db, "source_identity", identity)
    setmeta(db, "initialized", "1")
    setmeta(db, "repo", config["repo"])
    setmeta(db, "checked_at", start)
    return baseline, events, raw


def atomic_write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(value)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def report_text(repo, stamp, events):
    counts = {k: sum(e["kind"] == k for e in events) for k in ("issue", "comment", "commit", "branch")}
    lines = [f"# {repo} 更新", "", stamp,
             f'Issues: {counts["issue"]} | 评论: {counts["comment"]} | Commits: {counts["commit"]} | 分支变化: {counts["branch"]}', ""]
    for e in events:
        lines.extend([f'## [{e["kind"]} · {e["action"]}] {e["title"]}', "", e["url"], ""])
        if e.get("matched_files"):
            lines.extend(["匹配的文件：", *["- " + file_label(file) for file in e["matched_files"]], ""])
        if e.get("before") is not None:
            lines.extend(["变化前：", "```json", json.dumps(e["before"], ensure_ascii=False, indent=2), "```", ""])
        if e.get("after") is not None:
            lines.extend(["变化后：", "```json", json.dumps(e["after"], ensure_ascii=False, indent=2), "```", ""])
        else:
            lines.extend([e.get("body", ""), ""])
    return clean("\n".join(lines)) + "\n"


def file_label(file):
    previous = file.get("previous_filename")
    name = (previous + " → " if previous else "") + file["filename"]
    return f'{name} ({file.get("status", "changed")})'


def terminal_notify(report, events, directory):
    summary = report.with_suffix(".summary.txt")
    # Generated data is read as a file, never interpolated into a shell command.
    lines = ["LML 更新", ""]
    for event in events:
        lines.append(f'[{event["kind"]} · {event["action"]}] {event["title"].splitlines()[0]}')
        files = event.get("matched_files", [])
        lines.extend("  " + file_label(file) for file in files[:10])
        if len(files) > 10:
            lines.append(f"  另有 {len(files) - 10} 个匹配文件，见详情")
    lines.extend(["", "详情文件：" + str(report)])
    atomic_write(summary, clean("\n".join(lines)) + "\n")
    if sys.platform == "win32":
        windows_notify(Path(__file__).resolve(), summary)
        return
    if sys.platform != "darwin":
        raise RuntimeError("此系统请使用 notify=stdout 或 file")
    command = directory / (report.stem + ".command")
    script = Path(__file__).resolve()
    atomic_write(command, "#!/bin/zsh\n" + " ".join(shlex.quote(s) for s in
                 [sys.executable, str(script), "display", str(summary)]) +
                 "\nprintf '\\n按回车结束\\n'\nread answer\n")
    command.chmod(0o700)
    subprocess.run(["/usr/bin/open", "-a", "Terminal", str(command)], check=True, timeout=20)


def check(config, notify=None, api=None, source=None):
    root = Path(config["data_dir"]).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with process_lock(root / "check.lock") as acquired:
        if not acquired:
            print("已有检查正在运行，本次跳过。")
            return
        db = connect(root / "state.sqlite")
        try:
            stamp = now()
            with db:
                baseline, events, raw = collect(db, source or (GitHubSource(api) if api else create_source(config)), config, stamp)
                if events:
                    batch = stamp.replace(":", "-") + "-" + hashlib.sha256(
                        json.dumps(events, sort_keys=True).encode()).hexdigest()[:10]
                    report = root / "reports" / (batch + ".md")
                    atomic_write(report, report_text(config["repo"], stamp, events))
                    atomic_write(root / "raw" / (batch + ".json"), json.dumps(
                        {"repo": config["repo"], "checked_at": stamp, "events": events, "responses": raw},
                        ensure_ascii=False, indent=2))
                    db.execute("INSERT OR IGNORE INTO batches(id,report) VALUES (?,?)", (batch, str(report)))
            print("已建立基线，不提示历史数据。" if baseline else f"检查完成，发现 {len(events)} 项更新。")
            mode = notify or config.get("notify", "terminal")
            for batch, report in db.execute("SELECT id,report FROM batches WHERE notified=0 ORDER BY id").fetchall():
                raw_file = root / "raw" / (batch + ".json")
                batch_events = json.loads(raw_file.read_text(encoding="utf-8"))["events"]
                if mode == "none":
                    print("待提示报告：", report)
                    continue
                if mode == "terminal":
                    terminal_notify(Path(report), batch_events, root / "notifications")
                elif mode == "file":
                    print("已保存报告：", report)
                elif mode == "stdout":
                    print(clean(Path(report).read_text(encoding="utf-8")))
                else:
                    raise ValueError("notify 必须是 terminal、stdout、file 或 none")
                with db:
                    db.execute("UPDATE batches SET notified=1 WHERE id=?", (batch,))
        finally:
            db.close()


def load_config(path):
    config = json.loads(path.read_text(encoding="utf-8"))
    include_paths(config)
    source_identity(config)
    if config.get("provider", "github") not in ("github", "gitlab"):
        raise ValueError("provider 目前支持 github 或 gitlab")
    if config.get("notify", "terminal") not in ("terminal", "stdout", "file", "none"):
        raise ValueError("notify 必须是 terminal、stdout、file 或 none")
    for section, name in (("auth", "token_file"), ("network", "ca_file")):
        value = config.get(section, {}).get(name)
        if value:
            config[section][name] = str((path.parent / Path(os.path.expandvars(value)).expanduser()).resolve())
    config["data_dir"] = str((path.parent / Path(config.get("data_dir", "data")).expanduser()).resolve())
    return config


def install(config_path, config):
    if sys.platform == "win32":
        return install_windows(config_path, config)
    if sys.platform != "darwin":
        raise RuntimeError("此系统请通过外部调度器执行 check")
    interval = int(config.get("interval_seconds", 300))
    if interval < 60:
        raise ValueError("检查间隔至少 60 秒")
    label = "com.dawnyoake.lml-monitor"
    logs = Path(config["data_dir"]) / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    agent = Path.home() / "Library/LaunchAgents" / (label + ".plist")
    agent.parent.mkdir(parents=True, exist_ok=True)
    payload = {"Label": label, "ProgramArguments": [sys.executable, str(Path(__file__).resolve()),
               "--config", str(config_path), "check"], "StartInterval": interval, "RunAtLoad": True,
               "ProcessType": "Background", "StandardOutPath": str(logs / "stdout.log"),
               "StandardErrorPath": str(logs / "stderr.log"),
               "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"}}
    atomic_write(agent, plistlib.dumps(payload).decode())
    target = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", target + "/" + label], capture_output=True)
    subprocess.run(["launchctl", "bootstrap", target, str(agent)], check=True)
    print("已安装：", agent)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.json"))
    parser.add_argument("--log-file", type=Path, help="将 check 的输出和错误追加到 UTF-8 日志")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("check")
    p.add_argument("--notify", choices=["terminal", "stdout", "file", "none"])
    p = sub.add_parser("display")
    p.add_argument("file", type=Path)
    p.add_argument("--pause", action="store_true")
    sub.add_parser("install")
    sub.add_parser("uninstall")
    args = parser.parse_args()
    if args.command == "display":
        if sys.stdout and hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        print(clean(args.file.read_text(encoding="utf-8")))
        if args.pause:
            input("\n按回车关闭窗口…")
        return
    if args.command == "uninstall":
        if sys.platform == "win32":
            uninstall_windows()
            return
        label = "com.dawnyoake.lml-monitor"
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"], capture_output=True)
        (Path.home() / "Library/LaunchAgents" / (label + ".plist")).unlink(missing_ok=True)
        print("已卸载定时任务，保留数据。")
        return
    config_path = args.config.expanduser().resolve()
    def perform():
        config = load_config(config_path)
        if args.command == "install":
            install(config_path, config)
        else:
            check(config, args.notify)
    if args.log_file:
        args.log_file.parent.mkdir(parents=True, exist_ok=True)
        with args.log_file.open("a", encoding="utf-8", buffering=1) as log:
            with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
                try:
                    perform()
                except Exception as e:
                    print(f"{now()} 检查失败：{e}", file=sys.stderr)
                    return 1
    else:
        perform()


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except (RuntimeError, ValueError, OSError, urllib.error.URLError, subprocess.SubprocessError) as e:
        print(f"{now()} 检查失败：{e}", file=sys.stderr)
        sys.exit(1)
