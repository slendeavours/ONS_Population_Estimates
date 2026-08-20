"""Compare untracked work in the stale outer checkout against the live repo."""
import subprocess
import os
import hashlib
import collections

OUTER = r"C:\Users\slewi\ucws-repo"
LIVE = os.path.join(OUTER, "ONS_Population_Estimates")
SKIP_DIRS = {"__pycache__", ".git", "node_modules", ".claude", ".vscode"}

os.chdir(OUTER)
out = subprocess.run(["git", "status", "--porcelain"],
                     capture_output=True, text=True).stdout
untracked = [l[3:].strip().rstrip("/") for l in out.splitlines()
             if l.startswith("??")]


def walk(p):
    if os.path.isfile(p):
        return [p.replace(os.sep, "/")]
    found = []
    for root, dirs, fs in os.walk(p):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in fs:
            found.append(os.path.join(root, f).replace(os.sep, "/"))
    return found


def md5(p):
    try:
        with open(p, "rb") as fh:
            return hashlib.md5(fh.read()).hexdigest()
    except OSError:
        return None


same, diff, new = [], [], []
for u in untracked:
    if u == "ONS_Population_Estimates":
        continue
    for rel in walk(u):
        target = os.path.join(LIVE, rel).replace(os.sep, "/")
        if os.path.exists(target):
            (same if md5(rel) == md5(target) else diff).append(rel)
        else:
            new.append(rel)

print("identical copy already in live repo : %d" % len(same))
for s in same[:12]:
    print("   ", s)
print()
print("present in live repo but DIFFERENT  : %d" % len(diff))
for s in diff[:20]:
    print("   ", s)
print()
print("absent from live repo, must move    : %d" % len(new))
c = collections.Counter(x.split("/")[0] for x in new)
for k, v in c.most_common(30):
    print("   %-46s %d" % (k, v))
