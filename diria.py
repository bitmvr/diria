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
from urllib.parse import unquote, urljoin

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
    selected: set[str]
    cache: dict[str, tuple[list[FileInfo], list[DirInfo]]]

    @classmethod
    def create(cls, base_url: str) -> NavState:
        return cls(
            current_url=base_url,
            stack=[{"name": "Root", "url": base_url}],
            selected=set(),
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


def build_menu_choices(
    files: list[FileInfo],
    directories: list[DirInfo],
    nav: NavState,
) -> tuple[list[str], list[Action]]:
    """Build parallel lists of display strings and action tuples."""
    choices: list[str] = []
    actions: list[Action] = []

    if not nav.at_root:
        choices.append("Go Back")
        actions.append(("back", None))

    for dir_info in directories:
        choices.append(dir_info["name"])
        actions.append(("dir", dir_info))

    for file_info in files:
        marker = "[✓]" if file_info["url"] in nav.selected else "[ ]"
        choices.append(f"{marker} {file_info['name']}")
        actions.append(("file", file_info))

    if files:
        current_file_urls = {f["url"] for f in files}
        all_selected = current_file_urls.issubset(nav.selected)
        label = "Deselect All in Directory" if all_selected else "Select All in Directory"
        choices.append(label)
        actions.append(("sel", None))

    if nav.selected:
        choices.append(f"View Selected ({len(nav.selected)} files)")
        actions.append(("view", None))

    choices.append(f"Finish selecting ({len(nav.selected)} files)")
    actions.append(("done", None))

    if not files and not directories:
        choices.insert(0, "(empty directory)")
        actions.insert(0, ("noop", None))

    return choices, actions


def handle_selection(
    action: Action,
    files: list[FileInfo],
    nav: NavState,
) -> bool:
    """Handle menu selection. Returns True if should exit browse loop."""
    kind, payload = action

    if kind == "noop":
        return False

    if kind == "done":
        return True

    if kind == "view":
        print("\n=== Selected Files ===")
        for i, url in enumerate(sorted(nav.selected), 1):
            print(f"{i}. {unquote(url)}")
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

    if kind == "file":
        file_url = payload["url"]  # type: ignore[index]
        if file_url in nav.selected:
            nav.selected.remove(file_url)
        else:
            nav.selected.add(file_url)
        return False

    return False


def browse_and_select() -> list[str]:
    """Browse directories and select files."""
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
                    return []
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
            title=f"Location: {nav.path_display}",
            menu_highlight_style=("standout",),
            cursor_index=cursor_index,
        )
        index = menu.show()

        if index is None:  # User cancelled
            return []

        cursor_index = index
        should_exit = handle_selection(actions[index], files, nav)

        if should_exit:
            break

    return list(nav.selected)


def confirm_download(selected_urls: list[str]) -> bool:
    """Ask user to confirm the selected files."""
    if not selected_urls:
        print("No files selected. Aborting.")
        return False

    print("\nSelected files for download:")
    for url in selected_urls:
        print(unquote(url))
    print()

    menu = TerminalMenu(["Yes", "No"], title="Approve downloading these files?")
    return menu.show() == 0


def download_files(selected_urls: list[str]) -> None:
    """Download selected files using aria2c."""
    if not selected_urls:
        return

    if not check_aria2c():
        print("Error: aria2c is not installed or not in PATH.")
        print("Install it with: brew install aria2 (macOS) or apt install aria2 (Linux)")
        sys.exit(1)

    username = CONFIG.get("username")
    password = CONFIG.get("password")

    # Write URLs (and per-URL auth, if any) to a 0600 temp file so credentials
    # don't appear in argv / process listings.
    lines: list[str] = []
    for url in selected_urls:
        lines.append(url)
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
        selected_urls = browse_and_select()

        if confirm_download(selected_urls):
            download_files(selected_urls)
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
