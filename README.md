# HookGitlabRepoFile

Monitor GitLab or GitHub issues, comments and commits without cloning the monitored repository. Limit commit notifications to selected files or directories. Reports are saved as UTF-8 Markdown and JSON.

Primary deployment target: **Windows PC + an internal GitLab instance**. macOS + GitHub is also supported. Despite the repository name, the current implementation uses scheduled API polling, not a webhook listener.

The runtime uses only the Python standard library. Install standard CPython 3.11 or later; Windows scheduling also requires its `pythonw.exe`. Git, GitHub CLI and an LML checkout are not runtime dependencies. GitHub CLI is an optional GitHub credential source.

## Quick start

For internal GitLab on Windows, follow [the deployment guide](MIGRATION-WINDOWS-GITLAB.md).

```powershell
Copy-Item config.gitlab.windows.example.json config.json
# Edit the API URL, project, branch, paths, token source and GitLab diff limit.
# Put the token in the configured file, or provision its environment variable.
py -3 monitor.py check --notify stdout
py -3 monitor.py install
```

For GitHub, start with `config.github.example.json`. Run `python3 monitor.py check` to establish a baseline, then `python3 monitor.py install` to register the macOS LaunchAgent. A private GitHub repository requires credentials with repository read access.

The first check records a baseline without alerting on historical data. The default interval is five minutes. Desktop notifications run only while the user is logged in; sleeping machines cannot check immediately.

## Configuration

| Setting | Meaning |
|---|---|
| `provider` | `gitlab` or `github` |
| `api_base_url` | HTTPS API root, such as `https://gitlab.company.invalid/api/v4` |
| `repo` | GitLab project path/string ID, or GitHub `owner/name` |
| `branches` | Explicit branch names; an empty array follows the default branch |
| `include_paths` | Exact files or recursive directories ending in `/`; empty means all commits |
| `gitlab_diff_max_files` | Administrator-confirmed instance file-diff limit, required for GitLab path filtering |
| `interval_seconds` | Poll interval; Windows requires a whole number of minutes |
| `notify` | `terminal`, `stdout`, `file`, or `none` |
| `data_dir` | State and report directory, relative to the configuration file |
| `auth` | Token environment variable/file and optional GitHub CLI lookup |
| `network` | Timeout, corporate CA file and proxy configuration |

Example path rules:

```json
"include_paths": ["src/main.py", "config/", "docs/"]
```

Rules are case-sensitive and combined with OR. Directories match all nested files. There are no glob patterns. New, changed, deleted and renamed files are supported; either the old or new rename path can match. Issue/comment notifications and branch-history warnings are unaffected by file rules. Rule changes affect future checks and do not replay previously filtered commits or refilter existing pending batches.

GitLab may truncate file diffs at an instance-configured limit. Set the actual limit rather than guessing it. If a result reaches the configured limit, the monitor fails that check without advancing its cursor. See the deployment guide for other GitLab-specific limitations.

## Commands and outputs

```sh
python monitor.py --config config.json check
python monitor.py --config config.json check --notify stdout
python monitor.py --config config.json check --notify file
python monitor.py --config config.json install
python monitor.py uninstall
python -m unittest -v test_monitor test_migration
```

`terminal` opens a new console window on Windows or Terminal on macOS. It marks the batch notified once the launch request succeeds, not once a user has read it. `stdout` prints the report and marks it notified. `file` saves and acknowledges batches without a window. `none` saves batches and leaves them pending for a later notification attempt.

The configured data directory contains:

- `state.sqlite`: snapshots, commit identities, cursors and pending notifications.
- `reports/*.md`: readable batch details.
- `raw/*.json`: events and adapter records; GitLab records use normalized fields.
- `logs/`: scheduler logs.
- `notifications/*.command`: macOS console launchers; Windows starts Python directly.

Changing a source platform, API address or project requires a new data directory. Uninstalling removes the scheduled job and preserves reports. Back up data only while the monitor is stopped. Runtime data, tokens and local configuration are excluded from Git.

## Architecture and adaptation

- [AGENTS.md](AGENTS.md): instructions for agents working in this repository.
- [docs/AGENT-ADAPTATION.md](docs/AGENT-ADAPTATION.md): source contracts, extension points, migration procedure and verification limits.
- `sources.py`: transport, authentication and provider adapters.
- `monitor.py`: snapshots, path rules, deduplication, reports and CLI.
- `runtime.py`: file locks, Windows console launch and Windows task registration. The macOS installer currently remains in `monitor.py`.

The monitor detects final observed state, not every intermediate action. It does not monitor merge/pull requests or deleted comments. GitLab scans visible issues, including closed ones, and their recent notes, which can be costly for very large projects.

32 automated tests passed on macOS before the initial publication. GitLab responses and Windows-specific calls were tested with fixtures/mocks. **Internal GitLab connectivity, corporate authentication, Windows Task Scheduler and real desktop windows still require acceptance testing on the target PC.**
