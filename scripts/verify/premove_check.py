"""Safety check before removing the stale checkout.

Confirms every tracked file in the stale outer checkout has a counterpart in the
live repo, and reports the four untracked files that differ.
"""
import subprocess
import os
import hashlib

OUTER = r"C:\Users\slewi\ucws-repo"
LIVE = os.path.join(OUTER, "ONS_Population_Estimates")
os.chdir(OUTER)


def md5(p):
    try:
        with open(p, "rb") as fh:
            return hashlib.md5(fh.read()).hexdigest()
    except OSError:
        return None


tracked = subprocess.run(["git", "ls-files"], capture_output=True,
                         text=True).stdout.split()
missing, differs, identical = [], [], []
for rel in tracked:
    target = os.path.join(LIVE, rel)
    if not os.path.exists(target):
        missing.append(rel)
    elif md5(rel) == md5(target):
        identical.append(rel)
    else:
        differs.append(rel)

print("STALE TRACKED FILES: %d" % len(tracked))
print("  identical in live repo      : %d" % len(identical))
print("  differ from live repo       : %d" % len(differs))
print("  ABSENT from live repo       : %d" % len(missing))
if differs:
    print("\n  differing (live is 59 commits ahead, so live wins):")
    for d in differs[:25]:
        print("     ", d)
if missing:
    print("\n  *** ABSENT FROM LIVE - would be lost on delete ***")
    for m in missing:
        print("     ", m)

print("\nUNTRACKED FILES THAT DIFFER FROM LIVE:")
for rel in ["data/processed/cqc_locations_processed.csv", "scripts/_db.py",
            "scripts/register_lib.py", "scripts/s22_verify.py"]:
    target = os.path.join(LIVE, rel)
    is_tracked = subprocess.run(
        ["git", "-C", LIVE, "ls-files", "--error-unmatch", rel],
        capture_output=True, text=True).returncode == 0
    print("  %-46s live tracked=%s  outer=%s bytes  live=%s bytes"
          % (rel, is_tracked,
             os.path.getsize(rel) if os.path.exists(rel) else "-",
             os.path.getsize(target) if os.path.exists(target) else "-"))
