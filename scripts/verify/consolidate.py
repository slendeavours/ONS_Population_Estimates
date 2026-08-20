"""Move untracked work from the stale outer checkout into the live repo.

Run with --apply to execute; default is a dry run.

Only files absent from the live repo are moved. Files that exist in the live
repo are left behind for deletion, because the live checkout is 59 commits
ahead and its copy always wins.
"""
import subprocess
import os
import sys
import shutil
import hashlib
import collections

OUTER = r"C:\Users\slewi\ucws-repo"
LIVE = os.path.join(OUTER, "ONS_Population_Estimates")
APPLY = "--apply" in sys.argv
SKIP_DIRS = {"__pycache__", ".git", "node_modules", ".claude", ".vscode"}
# Local tooling and secrets stay at the outer level.
KEEP_AT_OUTER = {".env", "SKILLS", ".claude", ".vscode", ".gitleaks.toml"}

os.chdir(OUTER)
tracked = set(subprocess.run(["git", "ls-files"], capture_output=True,
                             text=True).stdout.split())
status = subprocess.run(["git", "status", "--porcelain", "--ignored"],
                        capture_output=True, text=True).stdout
roots = []
for line in status.splitlines():
    code, path = line[:2], line[3:].strip().rstrip("/")
    if code.strip() in ("??", "!!"):
        roots.append(path)

roots = [r for r in roots
         if r != "ONS_Population_Estimates" and r.split("/")[0] not in KEEP_AT_OUTER]


def walk(p):
    if os.path.isfile(p):
        return [p.replace(os.sep, "/")]
    out = []
    for root, dirs, fs in os.walk(p):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in fs:
            out.append(os.path.join(root, f).replace(os.sep, "/"))
    return out


def md5(p):
    try:
        with open(p, "rb") as fh:
            return hashlib.md5(fh.read()).hexdigest()
    except OSError:
        return None


move, skip_same, skip_live_wins = [], [], []
for r in roots:
    if not os.path.exists(r):
        continue
    for rel in walk(r):
        target = os.path.join(LIVE, rel.replace("/", os.sep))
        if os.path.exists(target):
            (skip_same if md5(rel) == md5(target) else skip_live_wins).append(rel)
        else:
            move.append(rel)

print("files to MOVE into live repo        : %d" % len(move))
c = collections.Counter(x.split("/")[0] for x in move)
for k, v in c.most_common(40):
    print("   %-46s %d" % (k, v))
print("\nleft behind, identical in live      : %d" % len(skip_same))
print("left behind, live copy is newer     : %d" % len(skip_live_wins))
for s in skip_live_wins:
    print("   ", s)

if not APPLY:
    print("\nDRY RUN. Re-run with --apply to move.")
    sys.exit(0)

moved = 0
for rel in move:
    target = os.path.join(LIVE, rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(target), exist_ok=True)
    shutil.move(rel, target)
    moved += 1
print("\nmoved %d files into %s" % (moved, LIVE))

# drop now-empty directories left behind in the outer checkout
removed = 0
for r in sorted(roots, key=len, reverse=True):
    if os.path.isdir(r):
        for root, dirs, fs in os.walk(r, topdown=False):
            if not os.listdir(root):
                os.rmdir(root)
                removed += 1
print("removed %d empty directories" % removed)
