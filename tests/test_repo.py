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


def _make_repo(tmp_path, n_files: int, body_lines: int = 50) -> tuple[str, list[str]]:
    body = "\n".join(f"def fn_{i}(): pass  # marker_line_{i}" for i in range(body_lines))
    for i in range(n_files):
        (tmp_path / f"mod_{i:03}.py").write_text(body + "\n")
    return str(tmp_path), repo.list_source_files(str(tmp_path))


def test_repo_map_keeps_headers_within_budget(tmp_path):
    root, files = _make_repo(tmp_path, n_files=3)
    m = repo.build_repo_map(root, files, max_chars=100_000)
    assert "marker_line_0" in m          # header content present
    assert len(m) <= 100_000


def test_repo_map_degrades_to_tree_when_over_budget(tmp_path):
    root, files = _make_repo(tmp_path, n_files=40)
    full = repo.build_repo_map(root, files, max_chars=10_000_000)
    m = repo.build_repo_map(root, files, max_chars=len(full) // 10)
    assert len(m) <= len(full) // 10
    assert all(f in m for f in files)    # every path still listed
    assert "marker_line_0" not in m      # headers dropped
    assert "headers omitted" in m        # degradation is explicit, not silent


def test_repo_map_truncates_file_list_as_last_resort(tmp_path):
    root, files = _make_repo(tmp_path, n_files=200, body_lines=1)
    m = repo.build_repo_map(root, files, max_chars=3000)
    assert len(m) <= 3000
    assert "more files" in m             # truncation marker with count


def test_resolve_source_path_mode(tmp_path):
    (tmp_path / "x.py").write_text("y = 1\n")
    root, slug = repo.resolve_source(None, str(tmp_path), str(tmp_path / "_wd"))
    assert root == str(tmp_path)
    assert slug == os.path.basename(str(tmp_path)).lower()
