"""Filesystem/git access over the target repository.

Pure, network-only-for-clone helpers: resolve a repo (local path or shallow
GitHub clone), list source files (filtering vendored/generated noise), read a
file, regex-grep, and build a compact "repo map" for the orchestrator.
"""

import os
import re
import subprocess

SOURCE_EXT = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".rb", ".php",
    ".c", ".h", ".cpp", ".hpp", ".cs", ".scala", ".kt", ".swift", ".sh",
}
SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", "dist", "build", "vendor",
    "third_party", "__pycache__", ".pytest_cache", ".mypy_cache", "target",
    ".next", ".cache", "site-packages",
}
SKIP_SUFFIX = (".lock", ".min.js", ".min.css", ".map", ".d.ts")


def _slug(text: str) -> str:
    text = text.strip().lower().rstrip("/")
    base = os.path.basename(text) or text
    return re.sub(r"[^a-z0-9._-]+", "-", base).strip("-") or "repo"


def resolve_source(repo: str | None, path: str | None, workdir: str) -> tuple[str, str]:
    """Resolve audit target to (root_dir, slug).

    ``path`` → that local directory. ``repo`` "owner/name" → shallow clone into
    ``workdir/<name>``. Exactly one of ``repo``/``path`` must be given.
    """
    if bool(repo) == bool(path):
        raise ValueError("provide exactly one of repo or path")
    if path:
        root = os.path.abspath(path)
        if not os.path.isdir(root):
            raise FileNotFoundError(f"path not found: {root}")
        return root, _slug(root)
    # repo "owner/name"
    if repo.count("/") != 1:
        raise ValueError(f"--repo must be 'owner/name', got: {repo!r}")
    name = repo.split("/")[1]
    dest = os.path.join(os.path.abspath(workdir), _slug(name))
    if not os.path.isdir(dest):
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        url = f"https://github.com/{repo}"
        subprocess.run(
            ["git", "clone", "--depth", "1", url, dest],
            check=True, capture_output=True, text=True,
        )
    return dest, _slug(name)


def list_source_files(root: str) -> list[str]:
    """Repo-relative source files, vendored/generated noise filtered out."""
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            if os.path.splitext(fn)[1] not in SOURCE_EXT:
                continue
            if fn.endswith(SKIP_SUFFIX):
                continue
            rel = os.path.relpath(os.path.join(dirpath, fn), root)
            found.append(rel.replace(os.sep, "/"))
    return sorted(found)


def read_file(root: str, rel: str, max_chars: int = 60000) -> str:
    """Read a repo-relative file, truncated to ``max_chars``."""
    full = os.path.join(root, rel)
    if not os.path.isfile(full):
        return f"[error: {rel} not found]"
    try:
        with open(full, "r", encoding="utf-8", errors="replace") as f:
            data = f.read(max_chars + 1)
    except OSError as exc:
        return f"[error reading {rel}: {exc}]"
    if len(data) > max_chars:
        data = data[:max_chars] + "\n... [truncated]"
    return data


def grep(root: str, pattern: str, path: str | None = None, max_hits: int = 80) -> list[dict]:
    """Regex search across the repo (or a single file). Returns [{file,line,text}]."""
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        return [{"file": "", "line": 0, "text": f"[invalid regex: {exc}]"}]
    targets = [path] if path else list_source_files(root)
    hits: list[dict] = []
    for rel in targets:
        full = os.path.join(root, rel)
        if not os.path.isfile(full):
            continue
        try:
            with open(full, "r", encoding="utf-8", errors="replace") as f:
                for i, line in enumerate(f, 1):
                    if rx.search(line):
                        hits.append({"file": rel, "line": i, "text": line.rstrip()[:300]})
                        if len(hits) >= max_hits:
                            return hits
        except OSError:
            continue
    return hits


def _file_size(root: str, rel: str) -> int:
    try:
        return os.path.getsize(os.path.join(root, rel))
    except OSError:
        return 0


def _render_map(root: str, files: list[str], header_lines: int, note: str) -> str:
    out = [f"Repository map — {len(files)} source files.{note}\n"]
    for rel in files:
        size = _file_size(root, rel)
        if header_lines:
            out.append(f"=== {rel} ({size} bytes) ===")
            head = read_file(root, rel, max_chars=8000).splitlines()[:header_lines]
            out.append("\n".join(head))
            out.append("")
        else:
            out.append(f"{rel} ({size} bytes)")
    return "\n".join(out)


def build_repo_map(root: str, files: list[str], header_lines: int = 40,
                   max_chars: int = 200_000) -> str:
    """Compact text view of the repo for the orchestrator: path + size + headers.

    Degrades to stay within ``max_chars`` (≈ a token budget) rather than blowing
    the orchestrator's context on large repos: full headers → short headers →
    tree-only (paths + sizes) → truncated tree. Degradation is announced in the
    map itself so the orchestrator knows what it is (not) seeing.
    """
    for lines, note in ((header_lines, ""),
                        (10, " (showing first 10 lines per file to fit context budget)"),
                        (0, " (file headers omitted to fit context budget)")):
        m = _render_map(root, files, lines, note)
        if len(m) <= max_chars:
            return m

    # Last resort: tree-only, truncated file list.
    head = f"Repository map — {len(files)} source files (file headers omitted to fit context budget).\n"
    out, used = [head], len(head)
    for i, rel in enumerate(files):
        line = f"{rel} ({_file_size(root, rel)} bytes)"
        if used + len(line) + 80 > max_chars:  # reserve room for the trailer
            out.append(f"... and {len(files) - i} more files (map truncated to fit context budget)")
            break
        out.append(line)
        used += len(line) + 1
    return "\n".join(out)
