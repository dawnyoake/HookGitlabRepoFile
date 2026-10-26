# Agent adaptation guide

## 1. Start with the deployment boundary

The monitored repository and this source checkout are different projects. The monitor needs API access only; never introduce a dependency on a local target checkout to implement path filtering.

The primary target is Windows + internal GitLab. The repository also supports macOS + GitHub. An existing installation may run from a separate directory. Editing this checkout does not deploy it, change that installation's config or update its scheduled job.

Before a deployment adaptation, identify:

1. GitLab hostname, deployment subpath, server version and project path/ID.
2. Branches, file/directory rules, and whether issue/comment reminders are desired.
3. Runtime user, Python installation, desktop versus unattended execution.
4. Credential provisioning, corporate CA, VPN/proxy and permitted API access.
5. Instance `diff_max_files`, and expected issue/commit volume.

Do not ask for tokens in chat. Provision a token file or named environment variable locally. GitLab is not GitHub-compatible merely because both host Git repositories.

## 2. Module map

| Module | Owns | Typical adaptations |
|---|---|---|
| `sources.py` | HTTPS transport, credentials, pagination, source identity and provider field normalization | Internal API root, custom GitLab API, alternate authentication, new provider |
| `monitor.py` | Snapshots, event comparison, branch cursors, SHA deduplication, path matching, batching, reports, CLI | New monitoring rules, report fields, notification dispatch |
| `runtime.py` | Platform locks, Windows console startup and Task Scheduler registration | Windows deployment policy, alternative desktop channel, service/desktop separation |
| `test_monitor.py` | Existing event/state/path tests with a fake GitHub client | Regression coverage for core behavior |
| `test_migration.py` | GitLab fixtures, transport/auth configuration and Windows mock calls | Company API samples and Windows command changes |

The macOS LaunchAgent installer and current notification dispatcher remain in `monitor.py`. A future notifier abstraction can be extracted at `terminal_notify`/the batch dispatch block; do not add a generic plugin framework unless a concrete integration needs it.

## 3. Source contract

`create_source(config)` returns an adapter implementing these methods. `check(..., source=fixture)` supports injecting an adapter for offline tests. The older `api=` injection wraps a fake GitHub transport and is retained for existing tests.

| Method | Return value |
|---|---|
| `issues(since)` | Iterator of normalized issues, including closed issues |
| `comments(since)` | Iterator of normalized issue comments |
| `default_branch()` | Branch-name string |
| `head(branch)` | Normalized commit at the branch head |
| `compare(before, after)` | Object with `status`, `commits`, `html_url` |
| `files(sha)` | Complete normalized changed-file list, or an explicit error |

Normalized issue fields:

```json
{
  "number": 7,
  "title": "Example issue",
  "body": "Description",
  "state": "open",
  "labels": [{"name": "backend"}],
  "assignees": [{"login": "developer"}],
  "milestone": {"number": 3},
  "html_url": "https://gitlab.company.invalid/team/project/-/issues/7"
}
```

`state` uses `open`/`closed` (normalize GitLab `opened` to `open`). `state_reason` is optional; `milestone` may be null. The issue number is the GitLab project-local IID, not its globally unique ID. Snapshot fields are normalized before comparison, so adapter changes can create apparent updates; account for this when changing the schema.

Normalized comment fields:

```json
{
  "id": 101,
  "issue_url": "/issues/7",
  "body": "Comment text",
  "updated_at": "2026-10-26T09:00:00Z",
  "user": {"login": "developer"},
  "html_url": "https://gitlab.company.invalid/team/project/-/issues/7#note_101"
}
```

The final component of `issue_url` must be the normalized issue number. Filter GitLab system Notes and GitHub PR records. Normalize timestamps with an explicit UTC offset; the engine overlaps its last successful check by five minutes.

Normalized commit and file:

```json
{
  "sha": "full-commit-sha",
  "commit": {"message": "Commit title\n\nCommit body"},
  "html_url": "https://gitlab.company.invalid/team/project/-/commit/full-commit-sha"
}
```

```json
{
  "filename": "new/path/file.txt",
  "previous_filename": "old/path/file.txt",
  "status": "renamed"
}
```

`previous_filename` is optional. Expected statuses include `added`, `modified`, `removed`, `renamed`. Both rename paths participate in matching. Compare returns commits reachable in the new head but not the old head. It returns `ahead`, `behind` or `diverged` for history classification. The current GitLab adapter makes forward and reverse comparisons; timeout/limits cause an error rather than guessing.

## 4. State invariants

- First check establishes a baseline without historical notifications. A newly added branch also establishes a head baseline.
- Track commits by SHA/ancestry, not timestamps. A recently pushed commit may have an old commit date.
- Unmatched commits still advance the branch cursor and are marked seen. This is necessary for quiet, efficient repeated checks.
- File-rule changes do not replay old commits. Deduplication is repository-wide: a SHA later reaching another monitored branch does not produce another commit event.
- A failed fetch, normalization or file-list request must roll back database updates. Preserve pending notifications after launch failures.
- Reports are written atomically before the transaction commits. A crash can leave an unregistered report; it must not mark unsaved updates as consumed.
- Source identity includes provider, normalized API root and project. Never silently reuse one source's database for another.
- A notified flag means delivery was requested, not that the user read it. `none` retains pending batches; `file` acknowledges them.
- Use the process lock for a single shared data directory. Do not place SQLite on a network share without separately assessing locking and reliability.

## 5. GitLab details that need care

GitLab uses `/api/v4/projects/<URL-encoded-project>/...`. Group/subgroup/project must be encoded as one project parameter, or a numeric project ID can be supplied as a string.

Ordinary issue comments are Notes. To avoid missing comments on old/closed issues, the adapter currently scans all visible issue identities and their recent Notes, sorted by update time. This costs at least one Notes request per issue per check. For large projects, a webhook ingestion service or a separately designed note index is preferable; do not restrict Notes checks to updated issues without proving that comments always update the issue timestamp on the target version.

GitLab commit diffs can silently truncate at an administrator-configured file count. `gitlab_diff_max_files` must match the actual instance value. The adapter rejects lists at or above it. A wrong, overly high value can hide truncation; obtain the real value. Binary/large diff text is not needed for path matching, but all filenames are.

The `raw/` JSON contains monitor events and adapter-normalized records. It is not a lossless archive of every upstream GitLab field. If a requirement needs audit-grade raw responses, extend the transport/archive layer explicitly.

## 6. Windows runtime details

`runtime.process_lock` conditionally imports `msvcrt` on Windows and locks one file byte. POSIX uses `fcntl`. Never import `fcntl` unconditionally.

The scheduled job starts `pythonw.exe` with `--log-file` to avoid a console flash on every poll. A matching update starts `python.exe` with `CREATE_NEW_CONSOLE`, passes a summary file as an argument, and pauses for Enter. This deliberately bypasses `cmd.exe` expansion of percent signs, ampersands or text from the repository.

`install_windows` registers a single current-user task named `LML-monitor` using `schtasks.exe /IT`. Desktop notifications require a logged-in interactive user. Moving the installation or changing the poll interval requires running install again. Task registration may be restricted by company policy. The installer does not embed a user password.

An unattended collector and desktop notifier are separate concerns. If a service is later required, keep collection/state in a background process and dispatch desktop notifications through a per-user process/queue. Changing the existing task to SYSTEM does not provide a visible user desktop.

## 7. Adaptation workflow

1. Copy an example config to ignored `config.json`; keep company values and credentials local.
2. Configure API/project/auth/network/paths and a new data directory. Only change the adapter if configuration is insufficient.
3. Add sanitized representative fixtures for any company-specific behavior. Never copy real confidential issues or tokens into tests.
4. Run the documented offline tests, compilation and `git diff --check`.
5. On the target machine, establish a baseline manually, then run explicitly authorized live tests for matching and nonmatching files, rename/delete, issue/comment updates, repeated checks, network recovery and scheduler execution.
6. Record exact versions, results and remaining limits. Distinguish fixture tests, API connectivity, scheduled execution and visually confirmed desktop behavior.

The initial implementation was tested on macOS. Windows subprocess/lock branches and GitLab APIs were exercised with mocks/fixtures. Do not claim actual company deployment or Windows desktop acceptance until those steps have run on the target PC.

References: [GitLab Issues](https://docs.gitlab.com/api/issues/), [Notes](https://docs.gitlab.com/api/notes/), [Commits](https://docs.gitlab.com/api/commits/), [Compare](https://docs.gitlab.com/api/repositories/#compare-branches-tags-or-commits), [Task Scheduler commands](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/schtasks-create).
