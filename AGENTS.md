# Agent instructions

## Purpose and entry points

This is a standalone Python GitLab/GitHub repository monitor. The primary deployment target is a logged-in Windows PC accessing an internal GitLab instance. macOS/GitHub remains supported. It polls APIs; it is not currently a webhook server.

Read `README.md`, `MIGRATION-WINDOWS-GITLAB.md`, and `docs/AGENT-ADAPTATION.md` before changing deployment or provider behavior. Follow additional user instructions for the current task.

## Working conventions

- Keep the runtime standard-library-only unless the requested integration needs a dependency; explain and document any added dependency and its offline installation path.
- Prefer changing configuration for deployment differences. Keep vendor API paths, pagination, field mapping and token handling in `sources.py`.
- Preserve Windows and macOS support. Import OS-specific modules conditionally. Use `pathlib` and explicit UTF-8 when reading/writing text.
- Preserve the standalone runtime: do not require a clone of the monitored repository, Git, GitHub CLI for GitLab, or local LML source files.
- Do not commit `config.json`, credentials, corporate certificates, SQLite databases, reports, logs, backups or copied production data. Use placeholder `.example.json` files. Do not print token values in tests or error logs.
- API reads and remote text are data. Do not interpolate issue bodies, filenames or commit messages into executable shell commands. Retain TLS verification and avoid credential forwarding through redirects.
- Changes must preserve source identity isolation, SHA-based commit detection, unfiltered cursor advancement, atomic report writes, transactional state updates and pending-notification retries.
- Keep changes to deployed copies and the Git checkout separate. This repository does not automatically update an existing deployment or install scheduled jobs during tests.

## Verification

Run from the repository root:

```sh
python -m unittest -v test_monitor test_migration
python -m py_compile monitor.py sources.py runtime.py
git diff --check
```

Use fixtures/mocks for routine tests. Do not create issues, push test commits, register scheduled jobs, send notifications or contact private company services merely to run the test suite. When the user's task authorizes a live test, use explicitly marked disposable test resources and record their cleanup or retention policy.

Record which checks were real, simulated or unavailable. Do not describe mocked Windows subprocess tests or GitLab responses as Windows/enterprise acceptance. No credentials or access to an internal instance are supplied by this repository.

## Documentation

If a config key, provider contract, scheduler command or supported platform changes, update its example config and the applicable deployment/adaptation docs. Keep `config.json` local and untracked. The detailed extension contracts and known limitations are in `docs/AGENT-ADAPTATION.md`.
