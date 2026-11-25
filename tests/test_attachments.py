import json
from datetime import UTC, datetime

import pytest

from investigate.attachments import (
    AttachmentError,
    Upload,
    sanitise_name,
    store_attachments,
)
from investigate.cli import main
from investigate.config import ConfigError, Depth, config_path, load_config
from investigate.report import ReportInput, render_report
from investigate.result import parse_result
from investigate.service import execute_run
from tests.fakes import FakeRunner

NOW = datetime(2026, 3, 4, 12, 0, tzinfo=UTC)


def _file(tmp_path, name, content=b"data"):
    path = tmp_path / "src" / name
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(content)
    return path


def test_sanitise_name():
    assert sanitise_name("../../etc/passwd") == "passwd"
    assert (
        sanitise_name("C:" + chr(92) + "Users" + chr(92) + "x" + chr(92) + "my file (1).PDF")
        == "my file_1.PDF"
    )
    assert sanitise_name("a<b>|c?.png") == "a_b_c.png"
    assert sanitise_name("a b") == "a b"
    assert sanitise_name("CON.txt") == "attachment-CON.txt"
    assert sanitise_name("Ünï.md") == "n.md"
    assert len(sanitise_name("x" * 500 + ".pdf")) <= 100


def test_store_copies_and_handles_collisions(tmp_path):
    a = _file(tmp_path, "report.pdf", b"one")
    other = tmp_path / "other"
    other.mkdir()
    b = other / "report.pdf"
    b.write_bytes(b"two")
    folder = tmp_path / "run"
    folder.mkdir()

    stored = store_attachments(
        folder, [a, b, Upload("REPORT.pdf", b"three"), Upload("shot.png", b"x")], 1000
    )

    assert [s.name for s in stored] == ["report.pdf", "report-2.pdf", "REPORT-3.pdf", "shot.png"]
    assert [s.is_image for s in stored] == [False, False, False, True]
    assert (folder / "attachments" / "report-2.pdf").read_bytes() == b"two"
    assert a.read_bytes() == b"one" and b.exists()  # copied, never moved


def test_store_rejects_oversize_missing_and_directories_before_copying(tmp_path):
    ok = _file(tmp_path, "ok.txt")
    big = _file(tmp_path, "big.bin", b"x" * 2_000_000)
    folder = tmp_path / "run"
    folder.mkdir()
    with pytest.raises(AttachmentError, match=r"big\.bin.*limit of 1 MB"):
        store_attachments(folder, [ok, big], 1_048_576)
    assert not (folder / "attachments").exists()
    with pytest.raises(AttachmentError, match="not found"):
        store_attachments(folder, [tmp_path / "nope.txt"], 1000)
    with pytest.raises(AttachmentError, match="not a file"):
        store_attachments(folder, [tmp_path], 1000)
    with pytest.raises(AttachmentError, match="over the limit"):
        store_attachments(folder, [Upload("p.png", b"xx")], 1)


def test_execute_run_with_attachments_and_no_text(home, tmp_path):
    pdf = _file(tmp_path, "paper.pdf", b"%PDF")
    runner = FakeRunner()
    result = execute_run(
        "", Depth.QUICK, home=home, runner=runner, now=NOW, attachments=[pdf, Upload("s.png", b"i")]
    )
    assert result.status == "completed"
    assert (result.folder / "attachments" / "paper.pdf").read_bytes() == b"%PDF"
    assert (result.folder / "attachments" / "s.png").is_file()
    assert result.folder.name.endswith("paper-pdf")
    prompt = runner.requests[0].prompt
    assert "attachments/paper.pdf" in prompt and "attachments/s.png" in prompt
    assert "no text" in prompt
    run = json.loads((result.folder / "run.json").read_text(encoding="utf-8"))
    assert [a["name"] for a in run["attachments"]] == ["paper.pdf", "s.png"]
    html = result.report_path.read_text(encoding="utf-8")
    assert '<img src="attachments/s.png"' in html
    assert 'href="attachments/paper.pdf"' in html


def test_oversize_attachment_creates_no_run(home, tmp_path):
    big = _file(tmp_path, "big.bin", b"x" * 2_000_000)
    home.mkdir()
    config_path(home).write_text("max_attachment_mb = 1\n", encoding="utf-8")
    with pytest.raises(AttachmentError):
        execute_run("t", Depth.QUICK, home=home, runner=FakeRunner(), attachments=[big])
    assert not (home / "runs").exists()


def test_config_limit(home):
    home.mkdir()
    assert load_config(home).max_attachment_mb == 25
    config_path(home).write_text("max_attachment_mb = 0.5\n", encoding="utf-8")
    assert load_config(home).max_attachment_mb == 0.5
    config_path(home).write_text("max_attachment_mb = -1\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="max_attachment_mb"):
        load_config(home)


def test_report_item_section_escapes_and_lists(tmp_path):
    from investigate.attachments import Attachment

    html = render_report(
        ReportInput(
            title="T",
            item="see https://example.com/a. and <script>alert(1)</script>",
            kind="investigation",
            depth="quick",
            status="completed",
            created="c",
            result=parse_result("x"),
            attachments=(Attachment('a"><b>.png', 5, True), Attachment("n.md", 3, False)),
        )
    )
    assert "<h2>Item</h2>" in html
    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html
    assert 'href="https://example.com/a"' in html
    assert "<b>.png" not in html
    assert html.count("<img ") == 1
    assert 'href="attachments/n.md"' in html


def test_cli_attach(home, tmp_path, capsys):
    pdf = _file(tmp_path, "a.pdf")
    shot = _file(tmp_path, "shot.png")
    runner = FakeRunner()
    assert main(["run", "look", "--attach", str(pdf), "--attach", str(shot)], runner=runner) == 0
    assert "attachments/a.pdf" in runner.requests[0].prompt
    assert pdf.exists() and shot.exists()
    assert main(["run", "--attach", str(pdf)], runner=FakeRunner()) == 0  # no text at all


def test_cli_errors(home, tmp_path, capsys):
    assert main(["run"], runner=FakeRunner()) == 2
    assert "--attach" in capsys.readouterr().err
    assert main(["run", "x", "--attach", str(tmp_path / "gone.pdf")], runner=FakeRunner()) == 2
    assert "not found" in capsys.readouterr().err


def test_sanitise_keeps_extension_of_non_ascii_stem():
    assert sanitise_name("Ü.png") == "file.png"
    assert sanitise_name("日本.pdf") == "file.pdf"
    assert sanitise_name("...") == "file"


def test_sanitise_reserved_name_before_first_dot():
    assert sanitise_name("nul.tar.gz") == "attachment-nul.tar.gz"
    assert sanitise_name("com1.txt") == "attachment-com1.txt"


def test_truncation_never_leaves_trailing_space_or_dot(tmp_path):
    assert sanitise_name("a.abcdefghi jkl") == "a.abcdefghi"
    assert not sanitise_name("x" * 98 + " y.pdf").split(".")[0].endswith(" ")
    folder = tmp_path / "run"
    folder.mkdir()
    stored = store_attachments(
        folder, [Upload("a.abcdefghi jkl", b"one"), Upload("a.abcdefghi", b"two")], 1000
    )
    assert [s.name for s in stored] == ["a.abcdefghi", "a-2.abcdefghi"]
    contents = sorted(p.read_bytes() for p in (folder / "attachments").iterdir())
    assert contents == [b"one", b"two"]


def test_unreadable_source_is_rejected_up_front(tmp_path, monkeypatch):
    locked = _file(tmp_path, "locked.pdf")
    real_open = type(locked).open

    def deny(self, *args, **kwargs):
        if self == locked:
            raise PermissionError("denied")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(type(locked), "open", deny)
    folder = tmp_path / "run"
    folder.mkdir()
    with pytest.raises(AttachmentError, match="cannot read.*locked.pdf"):
        store_attachments(folder, [locked], 1000)


def test_copy_failure_fails_cleanly_and_leaves_no_run_folder(home, tmp_path, monkeypatch):
    src = _file(tmp_path, "a.pdf")

    def boom(*args, **kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr("investigate.attachments.shutil.copyfile", boom)
    with pytest.raises(AttachmentError, match="a.pdf"):
        execute_run("t", Depth.QUICK, home=home, runner=FakeRunner(), attachments=[src])
    assert list((home / "runs").iterdir()) == []
    assert main(["run", "t", "--attach", str(src)], runner=FakeRunner()) == 2
