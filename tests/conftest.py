import pytest


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    """Every test gets its own data folder; nothing ever touches the real home folder."""
    path = tmp_path / "data"
    monkeypatch.setenv("INVESTIGATE_HOME", str(path))
    return path


@pytest.fixture(autouse=True)
def user_profile(tmp_path, monkeypatch):
    """`Path.home()` is a temp folder too, so a lookup that falls back to the real
    `~/.local/bin` (where the native Claude Code installer puts `claude`) finds nothing."""
    path = tmp_path / "userprofile"
    path.mkdir()
    monkeypatch.setenv("USERPROFILE", str(path))
    monkeypatch.setenv("HOME", str(path))
    return path
