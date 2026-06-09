import os

from src.tools import repo

ROOT = os.path.join(os.path.dirname(__file__), "fixtures", "fakerepo")


def test_list_filters_vendored_and_locks():
    files = repo.list_source_files(ROOT)
    assert "app.py" in files and "util.py" in files
    assert "pkg.lock" not in files
    assert not any(f.startswith("node_modules") for f in files)


def test_read_file():
    assert "run_command(" in repo.read_file(ROOT, "app.py")


def test_grep_finds_marker():
    hits = repo.grep(ROOT, r"run_command\(")
    assert any(h["file"] == "app.py" for h in hits)
    assert all({"file", "line", "text"} <= set(h) for h in hits)


def test_repo_map_lists_files():
    m = repo.build_repo_map(ROOT, repo.list_source_files(ROOT))
    assert "app.py" in m and "util.py" in m


def test_resolve_source_path_mode(tmp_path):
    (tmp_path / "x.py").write_text("y = 1\n")
    root, slug = repo.resolve_source(None, str(tmp_path), str(tmp_path / "_wd"))
    assert root == str(tmp_path)
    assert slug == os.path.basename(str(tmp_path)).lower()
