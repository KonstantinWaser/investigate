import pytest


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    """Every test gets its own data folder; nothing ever touches the real home folder."""
    path = tmp_path / "data"
    monkeypatch.setenv("INVESTIGATE_HOME", str(path))
    return path
