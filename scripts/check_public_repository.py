"""Check tracked publication inputs; not a secret scanner or a history scrubber."""
from pathlib import Path
import re
import subprocess


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    paths = subprocess.check_output(
        ["git", "ls-files", "-z"], cwd=root
    ).decode("utf-8").split("\0")
    private_roots = {".research", ".sourceledger", ".profiles", ".browser-cache",
                     ".tmp", "outputs", "workspace"}
    private_suffixes = {".xlsx", ".xls", ".db", ".sqlite", ".sqlite3"}
    plate = re.compile(r"(?<!\d)\d{2,3}[가-힣]\d{4}(?!\d)")
    failures = []
    for name in filter(None, paths):
        path = Path(name)
        if (path.parts[0] in private_roots or path.suffix.lower() in private_suffixes
                or path.name.endswith((".db-wal", ".db-shm", ".sqlite-wal", ".sqlite-shm",
                                      ".sqlite3-wal", ".sqlite3-shm"))):
            failures.append(f"{name}: private output must not be tracked")
        if name.startswith("tests/") and path.suffix in {".html", ".py"}:
            text = (root / path).read_text(encoding="utf-8")
            if plate.search(text):
                failures.append(f"{name}: replace vehicle plates with synthetic identifiers")
        if name.startswith("tests/fixtures/adapters/") and path.suffix == ".html":
            if "Synthetic regression fixture" not in (root / path).read_text(encoding="utf-8"):
                failures.append(f"{name}: label the synthetic regression fixture")
    for failure in failures:
        print(failure)
    if not failures:
        print("Tracked publication inputs passed the limited public-data checks.")
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
