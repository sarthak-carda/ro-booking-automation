"""
Resolves interpreter paths for adapters that need an isolated runtime,
rather than hardcoding a version-specific path that breaks the moment nvm
upgrades or the venv gets recreated.

Direct port of bakeoff/lib/resolveRuntime.js.

WHY isolated runtimes exist at all (real incident, not caution for its own
sake): the first `pip install browser-use` for this project landed in the
shared/system Python environment and upgraded packages other tools on this
machine depend on (openai, mcp, pydantic, ...) — pip itself flagged a real
conflict (`openai-agents` needs `mcp<2`, browser-use wants `mcp==2.1.1`).
That install was reverted and moved into `bakeoff/.venv` instead. Similarly,
Stagehand v4 requires Node >=22.18.0, but this project's own Node is v20 —
rather than bumping the system-wide default Node (which every other script
here relies on), a scoped Node 22 was installed via nvm and is resolved
on demand, only for the one adapter that needs it.
"""
import re
from pathlib import Path

BAKEOFF_ROOT = Path(__file__).resolve().parent.parent

_NODE_VERSION_RE = re.compile(r"^v(\d+)\.")


def venv_python() -> str:
    """Python interpreter inside bakeoff/.venv (created by `python3 -m venv .venv` + `pip install browser-use`)."""
    bin_path = BAKEOFF_ROOT / ".venv" / "bin" / "python3"
    if not bin_path.exists():
        raise RuntimeError(
            "bakeoff/.venv not found. Set it up with:\n"
            "  cd bakeoff && python3 -m venv .venv && ./.venv/bin/pip install browser-use==0.13.10"
        )
    return str(bin_path)


def node22_binary() -> str:
    """Highest installed Node >=22 under nvm, without touching the shell's default `node`."""
    nvm_node_dir = Path.home() / ".nvm" / "versions" / "node"
    if nvm_node_dir.exists():
        versions = []
        for entry in nvm_node_dir.iterdir():
            match = _NODE_VERSION_RE.match(entry.name)
            if match and int(match.group(1)) >= 22:
                versions.append(entry.name)
        # newest first, lexicographic is fine for this filter width (same as the JS version)
        versions.sort(reverse=True)
        if versions:
            return str(nvm_node_dir / versions[0] / "bin" / "node")
    raise RuntimeError(
        "No Node >=22 found under nvm (~/.nvm/versions/node). Stagehand v4 requires it. Install with:\n"
        '  export NVM_DIR="$HOME/.nvm" && source "$NVM_DIR/nvm.sh" && nvm install 22'
    )
