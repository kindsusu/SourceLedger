"""Browser profiles and research workspaces must never be committed (AGENTS.md)."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_gitignore_keeps_profiles_and_workspaces_untracked():
    entries = {line.strip() for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()}
    for required in (".sourceledger/", ".profiles/", "workspace/"):
        assert required in entries, f".gitignore must keep ignoring {required}"
