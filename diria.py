#!/usr/bin/env python3

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from simple_term_menu import TerminalMenu

try:
    import tomllib
except ImportError:
    import tomli as tomllib


CONFIG: dict = {}


def load_config() -> dict:
    """Load configuration from config.toml."""
    config_path = Path(__file__).parent / "config.toml"
    with open(config_path, "rb") as f:
        return tomllib.load(f)


class FileInfo(TypedDict):
    name: str
    url: str


class DirInfo(TypedDict):
    name: str
    url: str


@dataclass
class NavState:
    """Navigation state for the file browser."""

    current_url: str
    stack: list[DirInfo]
    selected: set[str]  # individually-selected file URLs
    selected_dirs: set[str]  # directory URLs marked for recursive download
    cache: dict[str, tuple[list[FileInfo], list[DirInfo]]]

    @classmethod
    def create(cls, base_url: str) -> NavState:
        return cls(
            current_url=base_url,
            stack=[{"name": "Root", "url": base_url}],
            selected=set(),
            selected_dirs=set(),
            cache={},
        )

    def go_back(self) -> None:
        if len(self.stack) > 1:
            self.stack.pop()
            self.current_url = self.stack[-1]["url"]

    def enter_dir(self, dir_info: DirInfo) -> None:
        self.stack.append(dir_info)
        self.current_url = dir_info["url"]

    @property
    def path_display(self) -> str:
        return " > ".join(n["name"] for n in self.stack)

    @property
    def at_root(self) -> bool:
        return len(self.stack) == 1


def get_exclude_pattern() -> str | None:
    """Build exclude pattern from config, with fallback to defaults."""
    patterns = CONFIG.get("exclude_patterns", [r".*\.meta", r"^\.\.\/"])
    if not patterns:
        return None
    return "|".join(f"({p})" for p in patterns)


def check_aria2c() -> bool:
    """Check if aria2c is available."""
    return shutil.which("aria2c") is not None


def fetch_urls(url: str) -> tuple[list[FileInfo], list[DirInfo]]:
    """Fetch and filter URLs from the given URL."""
    auth = None
    if CONFIG.get("username") and CONFIG.get("password"):
        auth = (CONFIG["username"], CONFIG["password"])

    print(f"Fetching {url}...", end=" ", flush=True)
    timeout = CONFIG.get("timeout", 10)
    response = requests.get(url, auth=auth, timeout=timeout)
    response.raise_for_status()
    print("done")

    soup = BeautifulSoup(response.text, "html.parser")

    files: list[FileInfo] = []
    directories: list[DirInfo] = []
    exclude_pattern = get_exclude_pattern()

    for a in soup.find_all("a", href=True):
        href_val = a["href"]
        href = href_val[0] if isinstance(href_val, list) else href_val
        if exclude_pattern and re.match(exclude_pattern, href):
            continue

        full_url = urljoin(url, href)
        display = unquote(href)

        if href.endswith("/"):
            directories.append({"name": display.rstrip("/"), "url": full_url})
        else:
            files.append({"name": display, "url": full_url})

    return files, directories


def fetch_with_retry(url: str) -> tuple[list[FileInfo] | None, list[DirInfo] | None]:
    """Fetch URLs with error handling and retry option."""
    while True:
        try:
            return fetch_urls(url)
        except requests.RequestException as e:
            print(f"\nError fetching {url}: {e}")
            menu = TerminalMenu(
                ["Retry", "Go Back", "Quit"], title="What would you like to do?"
            )
            choice = menu.show()
            if choice == 0:  # Retry
                continue
            elif choice == 1:  # Go Back
                return None, None
            else:  # Quit
                sys.exit(1)


Action = tuple[str, object]  # (kind, payload); kinds: back, dir, file, sel, view, done, noop

# Leading pad for non-file rows so their labels align with file labels
# (which sit after a 2-cell "✓ " / "· " marker).
# Note: markers that start with "[" and contain a single non-whitespace char
# (e.g. "[X]", "[✓]") are parsed by simple_term_menu as shortcut-key
# definitions, causing asymmetric rendering. A bare single-cell glyph sidesteps
# that entirely.
NON_FILE_PAD = "  "


def build_menu_choices(
    files: list[FileInfo],
    directories: list[DirInfo],
    nav: NavState,
) -> tuple[list[str], list[Action]]:
    """Build parallel lists of display strings and action tuples."""
    choices: list[str] = []
    actions: list[Action] = []

    if not nav.at_root:
        choices.append(f"{NON_FILE_PAD}Go Back")
        actions.append(("back", None))

    for dir_info in directories:
        marker = "✓" if dir_info["url"] in nav.selected_dirs else "·"
        choices.append(f"{marker} {dir_info['name']}/")
        actions.append(("dir", dir_info))

    for file_info in files:
        marker = "✓" if file_info["url"] in nav.selected else "·"
        choices.append(f"{marker} {file_info['name']}")
        actions.append(("file", file_info))

    if files:
        current_file_urls = {f["url"] for f in files}
        all_selected = current_file_urls.issubset(nav.selected)
        label = "Deselect All in Directory" if all_selected else "Select All in Directory"
        choices.append(f"{NON_FILE_PAD}{label}")
        actions.append(("sel", None))

    total = len(nav.selected) + len(nav.selected_dirs)
    if total:
        choices.append(f"{NON_FILE_PAD}View Selected ({total} items)")
        actions.append(("view", None))

    choices.append(f"{NON_FILE_PAD}Finish selecting ({total} items)")
    actions.append(("done", None))

    if not files and not directories:
        choices.insert(0, f"{NON_FILE_PAD}(empty directory)")
        actions.insert(0, ("noop", None))

    return choices, actions


def handle_selection(
    action: Action,
    key: str,
    files: list[FileInfo],
    nav: NavState,
) -> bool:
    """Handle menu selection. Returns True if should exit browse loop.

    key: "enter" activates (navigate/command); " " (space) toggles selection.
    """
    kind, payload = action

    # Space toggles selection on files and dirs; no-op elsewhere.
    if key == " ":
        if kind == "file":
            url = payload["url"]  # type: ignore[index]
            if url in nav.selected:
                nav.selected.remove(url)
            else:
                nav.selected.add(url)
        elif kind == "dir":
            url = payload["url"]  # type: ignore[index]
            if url in nav.selected_dirs:
                nav.selected_dirs.remove(url)
            else:
                nav.selected_dirs.add(url)
        return False

    # Enter: activate.
    if kind == "noop" or kind == "file":
        return False

    if kind == "done":
        return True

    if kind == "view":
        print("\n=== Selected ===")
        if nav.selected:
            print("Files:")
            for i, url in enumerate(sorted(nav.selected), 1):
                print(f"  {i}. {unquote(url)}")
        if nav.selected_dirs:
            print("Directories (recursive):")
            for i, url in enumerate(sorted(nav.selected_dirs), 1):
                print(f"  {i}. {unquote(url)}")
        print("=" * 40)
        input("Press Enter to continue...")
        return False

    if kind == "sel":
        current_file_urls = {f["url"] for f in files}
        if current_file_urls.issubset(nav.selected):
            nav.selected -= current_file_urls
        else:
            nav.selected |= current_file_urls
        return False

    if kind == "back":
        nav.go_back()
        return False

    if kind == "dir":
        nav.enter_dir(payload)  # type: ignore[arg-type]
        return False

    return False


def browse_and_select() -> NavState | None:
    """Browse directories and select files/dirs. Returns NavState, or None if cancelled."""
    nav = NavState.create(CONFIG["base_url"])
    last_url: str | None = None
    cursor_index = 0

    while True:
        cached = nav.cache.get(nav.current_url)
        if cached is None:
            raw_files, raw_dirs = fetch_with_retry(nav.current_url)
            if raw_files is None or raw_dirs is None:
                # Error recovery: user chose "Go Back"
                if not nav.at_root:
                    nav.go_back()
                    continue
                else:
                    return None
            cached = (raw_files, raw_dirs)
            nav.cache[nav.current_url] = cached

        files, directories = cached

        choices, actions = build_menu_choices(files, directories, nav)

        # Reset cursor on directory change; otherwise keep it where the user left it.
        if nav.current_url != last_url:
            cursor_index = 0
        last_url = nav.current_url
        cursor_index = min(cursor_index, len(choices) - 1)

        menu = TerminalMenu(
            choices,
            title=f"Location: {nav.path_display}  [Enter=open  Space=select]",
            menu_highlight_style=("standout",),
            cursor_index=cursor_index,
            accept_keys=("enter", " "),
        )
        index = menu.show()

        if index is None:  # User cancelled
            return None

        cursor_index = index
        key = menu.chosen_accept_key or "enter"
        should_exit = handle_selection(actions[index], key, files, nav)

        if should_exit:
            break

    return nav


def walk_directory(
    start_url: str,
    cache: dict[str, tuple[list[FileInfo], list[DirInfo]]],
) -> list[FileInfo]:
    """Walk a remote directory recursively; return every file found.

    Uses cached listings from browsing when available. Raises on any fetch error
    so the caller can abort the entire download.
    """
    collected: list[FileInfo] = []
    visited: set[str] = set()
    queue: list[str] = [start_url]
    while queue:
        url = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)
        if url in cache:
            files, dirs = cache[url]
        else:
            files, dirs = fetch_urls(url)
            cache[url] = (files, dirs)
        collected.extend(files)
        for d in dirs:
            if d["url"] not in visited:
                queue.append(d["url"])
    return collected


def local_path(base_url: str, file_url: str) -> str:
    """Relative local path for a file URL, mirroring its location under base_url."""
    base_path = urlparse(base_url).path
    file_path = urlparse(file_url).path
    if file_path.startswith(base_path):
        rel = file_path[len(base_path):]
    else:
        rel = file_path.lstrip("/")
    return unquote(rel)


def build_download_plan(nav: NavState) -> list[tuple[str, str]] | None:
    """Resolve selections (files + recursive dirs) to (url, rel_path) pairs.

    Returns None if a recursive walk fails; the caller should abort.
    """
    base_url = CONFIG["base_url"]
    plan: list[tuple[str, str]] = []
    seen: set[str] = set()

    for url in nav.selected:
        if url not in seen:
            seen.add(url)
            plan.append((url, local_path(base_url, url)))

    if nav.selected_dirs:
        print(f"\nWalking {len(nav.selected_dirs)} directory tree(s)...")
        for dir_url in sorted(nav.selected_dirs):
            try:
                files = walk_directory(dir_url, nav.cache)
            except requests.RequestException as e:
                print(f"Error walking {dir_url}: {e}", file=sys.stderr)
                print("Aborting download.", file=sys.stderr)
                return None
            for f in files:
                if f["url"] not in seen:
                    seen.add(f["url"])
                    plan.append((f["url"], local_path(base_url, f["url"])))

    return plan


def confirm_download(plan: list[tuple[str, str]]) -> bool:
    """Ask user to confirm the resolved download plan."""
    if not plan:
        print("No files selected. Aborting.")
        return False

    print(f"\n{len(plan)} file(s) will be downloaded:")
    for _, rel in plan:
        print(f"  {rel}")
    print()

    menu = TerminalMenu(["Yes", "No"], title="Approve downloading these files?")
    return menu.show() == 0


def download_files(plan: list[tuple[str, str]]) -> None:
    """Download files using aria2c. `plan` is a list of (url, relative_path)."""
    if not plan:
        return

    if not check_aria2c():
        print("Error: aria2c is not installed or not in PATH.")
        print("Install it with: brew install aria2 (macOS) or apt install aria2 (Linux)")
        sys.exit(1)

    username = CONFIG.get("username")
    password = CONFIG.get("password")

    # Write URLs, per-URL output paths (so local tree mirrors remote), and
    # optional auth into a 0600 temp file so credentials don't appear in argv.
    lines: list[str] = []
    for url, rel in plan:
        lines.append(url)
        lines.append(f"  out={rel}")
        if username and password:
            lines.append(f"  http-user={username}")
            lines.append(f"  http-passwd={password}")

    with tempfile.NamedTemporaryFile(mode="w", delete=False) as temp_file:
        temp_file.write("\n".join(lines))
        temp_file_path = temp_file.name

    connections = CONFIG.get("aria2c_connections", 16)
    splits = CONFIG.get("aria2c_splits", 16)
    download_dir = CONFIG.get("download_dir", ".")
    cmd = ["aria2c", "-i", temp_file_path, f"-d{download_dir}", f"-x{connections}", f"-s{splits}"]

    try:
        subprocess.run(cmd, check=True)
    finally:
        os.unlink(temp_file_path)


def main() -> None:
    global CONFIG
    try:
        CONFIG = load_config()
    except FileNotFoundError:
        config_path = Path(__file__).parent / "config.toml"
        print(f"Error: {config_path} not found.", file=sys.stderr)
        print("Copy config.toml.example to that path and fill in your values.", file=sys.stderr)
        sys.exit(1)

    if bool(CONFIG.get("username")) != bool(CONFIG.get("password")):
        print(
            "Warning: 'username' and 'password' must both be set for HTTP Basic Auth; "
            "ignoring partial credentials.",
            file=sys.stderr,
        )

    try:
        nav = browse_and_select()
        if nav is None:
            print("Aborting.")
            return

        plan = build_download_plan(nav)
        if plan is None:
            sys.exit(1)

        if confirm_download(plan):
            download_files(plan)
        else:
            print("Aborting.")
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(0)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
