"""Credential scan over files or a git range.

Written because the scan was being retyped from memory at each push. A control
reconstructed by hand every time is a control with a different definition every
time, and the one thing worse than no scan is a scan whose coverage nobody can
state.

Usage:
    python scripts/credential_scan.py --range origin/main..HEAD
    python scripts/credential_scan.py path/to/file [...]

Exit 0 clean, 1 on any hit.
"""
import argparse
import re
import subprocess
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parent.parent

# Each pattern is a shape that should never reach a public repository. The
# long-hex rule catches key material that carries no label; it also catches
# git SHAs, which is why full SHAs are excluded rather than the rule dropped.
PATTERNS = {
    "password literal":   r"(?i)password\s*[=:]\s*['\"][^'\"]{3,}",
    "key/secret/token":   r"(?i)(api[_-]?key|secret|token|passphrase)\s*"
                          r"[=:]\s*['\"][A-Za-z0-9_\-/+]{8,}",
    "database url":       r"(?i)(postgres(ql)?|mysql|mongodb)://[^\s'\"]+:"
                          r"[^\s'\"]+@",
    "aws access key":     r"AKIA[0-9A-Z]{16}",
    "private key block":  r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    "bearer token":       r"(?i)bearer\s+[A-Za-z0-9_\-\.]{20,}",
    "github token":       r"gh[pousr]_[A-Za-z0-9]{16,}",
    "jwt":                r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}",
    "long hex string":    r"\b[0-9a-fA-F]{40,}\b",
}

# Lines that match a pattern but are known not to be credentials. Kept
# explicit and narrow: an allowlist that grows by guesswork is how a scan
# stops scanning.
ALLOW = [
    re.compile(r"^\s*[-+]?\s*(#|--|//)"),          # commented-out example
    re.compile(r"(?i)password\s*[=:]\s*['\"](\*{3,}|x{3,}|<[^>]+>|\$\{)"),
    re.compile(r"(?i)(getenv|environ|ENV\.get|os\.environ)"),
]


def scan_text(text, label):
    hits = []
    for line_no, line in enumerate(text.splitlines(), 1):
        if any(a.search(line) for a in ALLOW):
            continue
        for name, pat in PATTERNS.items():
            m = re.search(pat, line)
            if m:
                hits.append((label, line_no, name, m.group(0)[:70]))
    return hits


def scan_range(rng):
    """Only ADDED lines in the range - what this push would publish."""
    diff = subprocess.run(["git", "-C", str(REPO), "diff", "-U0", rng],
                          capture_output=True, text=True,
                          errors="replace").stdout
    added, current = [], "?"
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:]
        elif line.startswith("+") and not line.startswith("+++"):
            added.append((current, line[1:]))
    hits = []
    for path, line in added:
        # Line numbers are not meaningful for an isolated added line, so the
        # path is the locator. Reporting a wrong number would be worse than
        # reporting none.
        for label, _, name, snippet in scan_text(line, path):
            hits.append((label, "added", name, snippet))
    return hits, len(added)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--range", dest="rng")
    ap.add_argument("paths", nargs="*")
    a = ap.parse_args()
    if not a.rng and not a.paths:
        sys.exit("give --range or one or more paths")

    hits, scanned = [], 0
    if a.rng:
        hits, scanned = scan_range(a.rng)
        print(f"credential scan: {scanned} added line(s) in {a.rng}")
    for p in a.paths:
        t = Path(p).read_text(encoding="utf-8", errors="replace")
        hits += scan_text(t, p)
        scanned += len(t.splitlines())
    if a.paths:
        print(f"credential scan: {len(a.paths)} file(s)")

    for label, ln, name, snippet in hits:
        print(f"  HIT [{name}] {label}:{ln}  {snippet}")
    if hits:
        print(f"\n{len(hits)} hit(s). DO NOT PUSH.")
        return 1
    print("CLEAN")
    return 0


if __name__ == "__main__":
    sys.exit(main())
