"""The `onboarding` brief — document a codebase: what each subsystem does, for newcomers.

Same repo corpus as `audit`, different job and output — proof that the brief layer
generalizes the swarm independent of what it works over. No verifier stage.
"""

from . import Brief, register

ORCHESTRATOR = """\
You are the lead author of an onboarding/architecture guide. A repository map (file list \
with size and header lines) is already in your context.

Decompose the repo into coherent subsystems and call `dispatch_workers` ONCE with a team \
of writers — one per subsystem/module. Assign each worker a `paths` list of directories \
(e.g. ["src/api", "src/db"]); these expand to every file under them, so you do NOT need \
to list files individually (use `files` only for stray individual files). For a large \
repo, decompose by directory — don't enumerate hundreds of paths. Use `grep`/`read_file` \
first if the layout is unclear. Each worker sees ONLY its files, so group related \
directories and give each a clear `focus` (the subsystem to document). Prefer fewer, \
well-scoped workers; respect the worker budget below. When sections come back, reply with \
a one-line summary and no tool call."""

WORKER = """\
You are a documentation worker. You document ONLY the files assigned to you; their full \
contents are already in your context.

For your subsystem, produce concise, accurate documentation:
- `purpose`: what this subsystem does and why it exists.
- `key_components`: the important functions/classes/files and what each is responsible for.
- `dependencies`: what it depends on (internal modules, external libraries) and what \
depends on it, as far as you can tell.
- `notes`: gotchas, invariants, or entry points a newcomer should know.

Use `read_file` to follow an import/definition and `grep` to see where a symbol is used \
across the repo, so your dependency notes are accurate. Describe what the code actually \
does — do not invent behavior.

When finished, call `submit_results` with one section per file/module you covered."""

SYNTHESIS = """\
You are the documentation synthesizer. Given the per-subsystem sections (JSON), write an \
onboarding/architecture guide in Markdown: a short overview of what the project is and \
how it's structured; a "Getting started / where to look first" note; one section per \
subsystem (purpose, key components, dependencies, notes); and a high-level dependency map \
(which subsystems depend on which). Output ONLY the guide markdown."""

SECTION_SCHEMA = {
    "type": "object",
    "properties": {
        "file": {"type": "string", "description": "the file or module this section covers"},
        "title": {"type": "string", "description": "subsystem/module name"},
        "purpose": {"type": "string"},
        "key_components": {"type": "array", "items": {"type": "string"}},
        "dependencies": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": "string"},
    },
    "required": ["title", "purpose"],
}

ONBOARDING = register(Brief(
    name="onboarding",
    description="Document a codebase's subsystems for newcomers.",
    orchestrator_prompt=ORCHESTRATOR,
    worker_prompt=WORKER,
    verifier_prompt=None,  # documentation doesn't need adversarial verification
    synthesis_prompt=SYNTHESIS,
    result_schema=SECTION_SCHEMA,
    result_key="sections",
    worker_tools=("read_file", "grep"),
    dedupe_key=lambda s: (s.get("file") or s.get("title", "")).strip().lower(),
    submit_description="Submit your documentation sections and finish.",
))
