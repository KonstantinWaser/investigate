"""No personal data in tracked files: real user-home paths, email addresses, and any word on a
private denylist.

The denylist is a text file outside the repo, one word or phrase per line (case-insensitive,
`#` starts a comment), named by `INVESTIGATE_DENYLIST`. Without the variable that part is
skipped, so the repo never has to hold the words it forbids. Owners put their own name, employer
and paths there and the verify command enforces it on their machine.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DENYLIST_ENV = "INVESTIGATE_DENYLIST"

# Folder names after a user-home prefix that are placeholders, not people.
PLACEHOLDER_USERS = {
    "you",
    "user",
    "users",
    "username",
    "name",
    "me",
    "someone",
    "example",
    "alice",
    "bob",
    "public",
    "default",
    "all",
    "runner",
    "ubuntu",
    "x",
}
PLACEHOLDER_EMAIL_DOMAINS = {
    "example.com",
    "example.org",
    "example.net",
    "users.noreply.github.com",
}

_NAME = r"([^\s\\/:*?\"<>|`'\]\)]+)"
HOME_PATTERNS = [
    re.compile(r"[A-Za-z]:[\\/]+Users[\\/]+" + _NAME, re.I),
    re.compile(r"(?<![\w.])/Users/" + _NAME),
    re.compile(r"(?<![\w.])/home/" + _NAME),
]
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})\b")
EMAIL_LOCAL_PLACEHOLDERS = {"noreply", "no-reply"}
# The licence names its copyright holder on purpose; the denylist does not apply to it.
DENYLIST_EXEMPT = {"LICENSE"}


def find_problems(name: str, text: str, denylist: list[str] = ()) -> list[str]:
    """What looks personal in one file's text, each as `file:line: what`."""
    found: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        where = f"{name}:{number}"
        for pattern in HOME_PATTERNS:
            for match in pattern.finditer(line):
                user = match.group(1).strip("<>{}$%").lower()
                if (
                    match.group(1).startswith(("<", "{", "$", "%", "~"))
                    or user in PLACEHOLDER_USERS
                ):
                    continue
                found.append(f"{where}: user-home path with a real-looking name")
        for match in EMAIL.finditer(line):
            local = match.group(0).split("@")[0].lower()
            if match.group(1).lower() in PLACEHOLDER_EMAIL_DOMAINS or local in (
                EMAIL_LOCAL_PLACEHOLDERS
            ):
                continue
            found.append(f"{where}: email address")
        lowered = line.lower()
        for word in () if name in DENYLIST_EXEMPT else denylist:
            if word in lowered:
                found.append(f"{where}: denylisted word")
    return found


def load_denylist(path: str | None) -> list[str]:
    if not path:
        return []
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return [w.strip().lower() for w in lines if w.strip() and not w.strip().startswith("#")]


def tracked_texts() -> dict[str, str]:
    names = subprocess.run(
        ["git", "ls-files", "-z"],  # noqa: S607
        cwd=ROOT,
        capture_output=True,
        check=True,
    ).stdout.decode("utf-8")
    texts: dict[str, str] = {}
    for name in filter(None, names.split("\0")):
        path = ROOT / name
        if not path.is_file():
            continue
        data = path.read_bytes()
        if b"\0" in data:
            continue  # binary
        texts[name] = data.decode("utf-8", errors="replace")
    return texts


# The sample strings are built from pieces so this file does not contain what it looks for.
DRIVE = "C:" + "\\" + "Users" + "\\"
POSIX_HOME = "/" + "home" + "/"
MAC_HOME = "/" + "Users" + "/"


def test_scanner_flags_real_looking_home_paths():
    assert find_problems("f", DRIVE + "jsmith" + "\\notes")
    assert find_problems("f", "see " + MAC_HOME + "jsmith" + "/notes")
    assert find_problems("f", "cd " + POSIX_HOME + "jsmith")
    assert find_problems("f", DRIVE.replace("\\", "/") + "jsmith/x")


def test_scanner_accepts_placeholder_home_paths():
    for text in (
        DRIVE + "<name>" + "\\x",
        DRIVE + "you" + "\\x",
        POSIX_HOME + "user" + "/x",
        MAC_HOME + "username" + "/x",
        "~/notes and $HOME" + "/x",
        "a/path" + MAC_HOME + "x",
    ):
        assert find_problems("f", text) == [], text


def test_scanner_flags_emails_but_not_placeholders():
    assert find_problems("f", "mail " + "jane" + "@" + "corp.test")
    assert find_problems("f", "jane" + "@" + "example.com") == []
    assert find_problems("f", "12345+jane" + "@" + "users.noreply.github.com") == []
    assert find_problems("f", "noreply" + "@" + "anthropic.com") == []


def test_scanner_flags_denylisted_words_case_insensitively():
    assert find_problems("f", "Written by Somebody Else", ["somebody else"])
    assert find_problems("f", "nothing here", ["somebody else"]) == []


def test_denylist_file_ignores_blank_lines_and_comments(tmp_path):
    path = tmp_path / "deny.txt"
    path.write_text("# mine\n\nFoo Bar\n  baz  \n", encoding="utf-8")
    assert load_denylist(str(path)) == ["foo bar", "baz"]
    assert load_denylist(None) == []


def test_tracked_files_hold_no_personal_data():
    texts = tracked_texts()
    assert "README.md" in texts, "git ls-files returned nothing; run from a checkout"
    denylist = load_denylist(os.environ.get(DENYLIST_ENV))
    problems = [p for name, text in texts.items() for p in find_problems(name, text, denylist)]
    assert problems == []


@pytest.mark.skipif(DENYLIST_ENV in os.environ, reason="only checks the unset case")
def test_without_the_env_var_the_denylist_is_empty():
    assert load_denylist(os.environ.get(DENYLIST_ENV)) == []


def test_the_licence_is_exempt_from_the_denylist_only():
    assert find_problems("LICENSE", "Copyright Somebody", ["somebody"]) == []
    assert find_problems("README.md", "Copyright Somebody", ["somebody"])
