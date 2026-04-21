# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Diria is a single-file Python CLI (`diria.py`) that browses Apache/nginx-style HTTP directory indexes in an interactive terminal menu, lets the user select files across nested directories, and hands the selected URLs to `aria2c` for parallel download. `aria2c` is a hard runtime dependency (checked via `shutil.which` in `check_aria2c`).

## Commands

```bash
# Install (editable) into the local venv
.venv/bin/pip install -e ".[dev]"

# Run the CLI (entry point defined in pyproject.toml: diria = "diria:main")
.venv/bin/diria
# or directly
.venv/bin/python diria.py

# Lint / format check (ruff is the only dev dependency, line-length=100, rules E/F/W/I)
.venv/bin/ruff check .
.venv/bin/ruff format .
```

There is no test suite.

## Architecture notes

- **Config location is module-relative, not CWD-relative.** `load_config()` reads `Path(__file__).parent / "config.toml"`, so the config must sit next to the installed `diria.py`. Running `diria` from an arbitrary directory will *not* pick up a `config.toml` in the CWD. When diria is installed via `pip install .`, the module is copied into site-packages — users typically run from the repo root or install editable (`pip install -e .`) so `config.toml` resolves to the repo copy.
- **`CONFIG` is a module-level global** populated at import time. Any code path that imports `diria` requires `config.toml` to already exist, or import will fail.
- **Directory vs file detection is purely lexical**: `fetch_urls` treats an `<a href>` ending in `/` as a directory and anything else as a file. This depends on the remote server producing a standard autoindex page.
- **`exclude_patterns` are matched against the raw `href`** (usually relative), not the fully qualified URL. Patterns are OR-joined into a single regex via `get_exclude_pattern`. Default excludes `.meta` files and the `../` parent link.
- **`NavState.stack` is the source of truth** for location; `current_url` is a cached mirror of `stack[-1]["url"]`. Always mutate via `enter_dir` / `go_back` to keep them in sync.
- **Selection state is a flat `set[str]` of absolute URLs** on `NavState.selected` — selections persist across directory navigation, which is the whole point of the tool.
- **Auth is HTTP Basic only**, applied in two places that must stay in lockstep: `requests.get(..., auth=...)` in `fetch_urls` and the `--http-user` / `--http-passwd` flags appended to the `aria2c` command in `download_files`.

## Conventions

- Type hints use `from __future__ import annotations` + PEP 604 unions. `TypedDict` (`FileInfo`, `DirInfo`) describes the shape of parsed links.
- `config.toml` is gitignored and contains credentials; only `config.toml.example` is tracked. Never commit a real `config.toml`.
