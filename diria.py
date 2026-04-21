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

    @classmethod
    def create(cls, base_url: str) -> NavState:
        return cls(
            current_url=base_url,
            stack=[{"name": "Root", "url": base_url}],
            selected=set(),
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


def build_menu_choices(
    files: list[FileInfo],
    directories: list[DirInfo],
    nav: NavState,
) -> tuple[list[str], int]:
    """Build the menu choices list. Returns choices and file_index_start."""
    choices: list[str] = []

    # Add "Go Back" option if not at root
    if not nav.at_root:
        choices.append("[..] Go Back")

    # Add directories
    for dir_info in directories:
        choices.append(f"[DIR] {dir_info['name']}")

    file_index_start = len(choices)

    # Add files (mark selected ones)
    for file_info in files:
        if file_info["url"] in nav.selected:
            choices.append(f"[*] {file_info['name']}")
        else:
            choices.append(f"[ ] {file_info['name']}")

    # Add select all/deselect all if there are files
    if files:
        current_file_urls = {f["url"] for f in files}
        all_selected_in_dir = current_file_urls.issubset(nav.selected)
        if all_selected_in_dir:
            choices.append("[SEL] Deselect All in Directory")
        else:
            choices.append("[SEL] Select All in Directory")

    # Add action options
    if nav.selected:
        choices.append(f"[VIEW] View Selected ({len(nav.selected)} files)")
    choices.append(f"[DONE] Finish selecting ({len(nav.selected)} files)")

    # Handle empty directory
    if not files and not directories:
        choices.insert(0, "(empty directory)")

    return choices, file_index_start


def handle_selection(
    choice: str,
    index: int,
    files: list[FileInfo],
    directories: list[DirInfo],
    nav: NavState,
    file_index_start: int,
) -> bool:
    """Handle menu selection. Returns True if should exit browse loop."""
    if choice == "(empty directory)":
        return False

    if choice.startswith("[DONE]"):
        return True

    if choice.startswith("[VIEW]"):
        print("\n=== Selected Files ===")
        for i, url in enumerate(sorted(nav.selected), 1):
            print(f"{i}. {unquote(url)}")
        print("=" * 40)
        input("Press Enter to continue...")
        return False

    if choice.startswith("[SEL]"):
        current_file_urls = {f["url"] for f in files}
        if "Deselect" in choice:
            nav.selected -= current_file_urls
        else:
            nav.selected |= current_file_urls
        return False

    if choice == "[..] Go Back":
        nav.go_back()
        return False

    if choice.startswith("[DIR]"):
        dir_name = choice.replace("[DIR] ", "")
        dir_info = next(d for d in directories if d["name"] == dir_name)
        nav.enter_dir(dir_info)
        return False

    if choice.startswith("[*]") or choice.startswith("[ ]"):
        file_index = index - file_index_start
        file_url = files[file_index]["url"]
        if file_url in nav.selected:
            nav.selected.remove(file_url)
        else:
            nav.selected.add(file_url)
        return False

    return False


def browse_and_select() -> list[str]:
    """Browse directories and select files."""
    nav = NavState.create(CONFIG["base_url"])

    while True:
        files, directories = fetch_with_retry(nav.current_url)

        # Handle "Go Back" from error recovery
        if files is None:
            if not nav.at_root:
                nav.go_back()
                continue
            else:
                return []

        choices, file_index_start = build_menu_choices(files, directories, nav)

        menu = TerminalMenu(
            choices,
            title=f"Location: {nav.path_display}",
            menu_highlight_style=("standout",),
        )
        index = menu.show()

        if index is None:  # User cancelled
            return []

        choice = choices[index]
        should_exit = handle_selection(
            choice, index, files, directories, nav, file_index_start
        )

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
