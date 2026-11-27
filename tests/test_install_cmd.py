"""`install.cmd` is the one file a Windows user downloads. It cannot import the package, so this
test reads it as text and checks that what it names still exists: the extras, the entry points,
the repository URL and the subcommand it runs."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from investigate import cli

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "install.cmd"
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
TEXT = SCRIPT.read_bytes().decode("utf-8")
SPEC = re.compile(
    r'uv tool install\b[^\r\n]*"(?P<name>[\w.-]+)\[(?P<extras>[^\]]*)\] @ (?P<url>\S+)"'
)
EXE = re.compile(r"%TOOLBIN%\\([\w-]+)\.exe")


def test_script_has_windows_line_endings_so_it_survives_a_raw_download():
    data = SCRIPT.read_bytes()
    assert b"\r\n" in data and b"\n" not in data.replace(b"\r\n", b"")
    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert re.search(r"^install\.cmd\s+-text\b", attributes, re.M)  # stored as is, CRLF in the blob


def test_the_tool_install_line_matches_the_package():
    found = SPEC.search(TEXT)
    assert found, 'no `uv tool install --force "name[extras] @ url"` line'
    assert found["name"] == PYPROJECT["project"]["name"]
    assert set(found["extras"].split(",")) == set(PYPROJECT["project"]["optional-dependencies"])
    assert "--force" in found.group(0)


def test_the_source_is_this_repository_and_needs_no_git():
    repo = PYPROJECT["project"]["urls"]["Repository"]
    url = SPEC.search(TEXT)["url"]
    assert url.startswith(repo + "/")
    assert not url.startswith("git+"), "a fresh Windows PC has no git"


def test_every_program_it_starts_from_the_tool_folder_is_a_declared_entry_point():
    project = PYPROJECT["project"]
    declared = set(project["scripts"]) | set(project["gui-scripts"])
    used = set(EXE.findall(TEXT))
    assert used and used <= declared


def test_the_subcommand_it_runs_exists_and_is_the_first_run():
    line = next(x for x in TEXT.splitlines() if "%TOOLBIN%" in x and "first-run" in x)
    argv = line.split(".exe", 1)[1].replace('"', "").split()
    assert cli._parser().parse_args(argv).command == "first-run"


def test_it_stays_inside_the_user_profile_and_asks_for_no_admin_rights():
    lowered = TEXT.lower()
    for forbidden in (
        "runas",
        "program files",
        "hklm",
        "setx",
        "/m ",
        "winget",
        "choco",
        "msiexec",
    ):
        assert forbidden not in lowered, forbidden
    assert "%userprofile%" in lowered


def test_it_pauses_on_failure_and_exits_non_zero():
    assert re.search(r"^:fail\b", TEXT, re.M | re.I)
    fail = TEXT[re.search(r"^:fail\b", TEXT, re.M | re.I).start() :]
    assert "pause" in fail and "exit /b 1" in fail


def test_it_asks_before_installing_claude_code_and_uses_the_native_installer():
    assert "https://claude.ai/install.ps1" in TEXT
    assert "set /p" in TEXT.lower()


def test_it_uses_the_official_uv_installer():
    assert "https://astral.sh/uv/install.ps1" in TEXT


def _line_of(needle: str) -> int:
    return next(i for i, x in enumerate(TEXT.splitlines()) if needle in x)


def test_it_stops_a_running_tray_before_it_replaces_the_program():
    stop = _line_of("stop-tray")
    assert stop < _line_of("uv tool install")
    line = TEXT.splitlines()[stop]
    assert "%TOOLBIN%" in line
    guard = next(x for x in TEXT.splitlines()[:stop][::-1] if x.startswith("if "))
    assert r'if not exist "%TOOLBIN%\investigate.exe"' in guard  # a first install has no program
    argv = line.split(".exe", 1)[1].replace('"', "").split()
    assert cli._parser().parse_args(argv).command == "stop-tray"
    # Only stop-tray's own "tray stays" code loops back to the user. An older install has no
    # stop-tray (argparse exits 2) and a broken one crashes (exit 1); both ask the user to quit
    # the tray icon if it shows and then go on to the install.
    from investigate.firstrun import TRAY_STAYS

    above, stays, other = TEXT.splitlines()[stop + 1 : stop + 4]
    assert above == f"if errorlevel {TRAY_STAYS + 1} goto tray_unknown"
    assert stays == f"if errorlevel {TRAY_STAYS} goto tray_stays"
    assert other == "if errorlevel 1 goto tray_unknown"
    assert TEXT.splitlines()[stop + 4] == ":install_tool"
    unknown = TEXT[re.search(r"^:tray_unknown\b", TEXT, re.M).start() :]
    unknown = unknown[: unknown.index("goto install_tool")]
    assert "choose quit" in unknown.lower() and "pause" in unknown
    assert "choose quit" in TEXT[re.search(r"^:tray_stays\b", TEXT, re.M).start() :].lower()


def test_the_tool_folder_is_known_before_the_tray_is_stopped():
    assert _line_of("uv tool dir --bin") < _line_of("stop-tray")


def test_it_puts_the_tool_folder_on_the_user_path_after_the_install():
    assert _line_of("uv tool install") < _line_of("uv tool update-shell") < _line_of("first-run")


def test_the_readme_links_the_release_asset_and_keeps_a_raw_fallback():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    repo = PYPROJECT["project"]["urls"]["Repository"]
    assert f"{repo}/releases/latest/download/install.cmd" in readme
    assert f"{repo}/raw/main/install.cmd" in readme
    assert "gh release create" in readme
