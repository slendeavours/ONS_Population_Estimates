"""The only sanctioned way to push this repository.

The order - scan, verify, then push - is not a preference. It has failed twice
by being remembered rather than enforced: once the scan ran after the push, and
once the suite ran in the same command as the push and its exit code was read
after the irreversible step had already happened. A check whose result is
observed after the action is documentation, not a control.

So the check and the action live in one place and cannot be separated.

    python scripts/push.py                 # gate, then push
    python scripts/push.py --dry-run       # gate only, never pushes
    python scripts/push.py --install-hook  # also gate a bare `git push`

Exit 2 from the verification suite means known-red only, and is allowed to
publish - that is the suite's own contract. Exit 1 blocks.
"""
import argparse
import subprocess
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parent.parent
PY = sys.executable
REMOTE, BRANCH = "origin", "main"


def run(cmd, **kw):
    return subprocess.run(cmd, cwd=str(REPO), capture_output=True, text=True,
                          errors="replace", **kw)


def step(n, title):
    print(f"\n[{n}] {title}")


def fail(msg):
    print(f"\nBLOCKED: {msg}")
    print("Nothing was pushed.")
    return 1


def gate(verbose=True):
    """Every check, in order. Returns 0 to proceed, non-zero to stop."""

    step(1, "working tree is clean")
    # The suite verifies what is on disk. If the tree is dirty, the state that
    # passed is not the state being pushed, and the gate proves nothing.
    st = run(["git", "status", "--porcelain", "--untracked-files=no"])
    if st.stdout.strip():
        print(st.stdout.rstrip())
        return fail("tracked files are modified. The suite would verify a "
                    "state that is not the one being pushed. Commit or stash "
                    "first.")
    print("    clean")

    step(2, "commits to be pushed")
    rng = f"{REMOTE}/{BRANCH}..HEAD"
    run(["git", "fetch", REMOTE, BRANCH])
    log = run(["git", "log", "--oneline", rng])
    commits = [l for l in log.stdout.splitlines() if l.strip()]
    if not commits:
        print("    nothing to push")
        return 3
    for c in commits:
        print(f"    {c}")

    step(3, "credential scan (before the push, never after)")
    scan = run([PY, "scripts/credential_scan.py", "--range", rng])
    print("    " + scan.stdout.strip().replace("\n", "\n    "))
    if scan.returncode != 0:
        return fail("credential scan found something.")

    step(4, "verification suite")
    suite = run([PY, "scripts/verify_source_registry.py"])
    tail = [l for l in suite.stdout.splitlines() if l.strip()][-3:]
    for l in tail:
        print("    " + l)
    print(f"    exit {suite.returncode}")
    if suite.returncode == 1:
        return fail("the verification suite exited 1. Read the output, fix the "
                    "gate, and run this again. The suite is not advisory.")
    if suite.returncode not in (0, 2):
        return fail(f"the suite exited {suite.returncode}, which is neither "
                    f"pass (0) nor known-red (2). Treated as a failure.")
    if suite.returncode == 2:
        print("    known-red only; publishing allowed by the suite's contract")

    step(5, "generated artefacts are not stale")
    readme = run([PY, "scripts/sync_readme_sources.py", "--check"])
    print("    " + readme.stdout.strip().splitlines()[-1])
    if readme.returncode != 0:
        return fail("the README source block is stale. Regenerate it with "
                    "scripts/sync_readme_sources.py, commit, and run again.")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="run every gate and stop, whatever the result")
    ap.add_argument("--install-hook", action="store_true",
                    help="install a pre-push hook so a bare git push is gated")
    a = ap.parse_args()

    if a.install_hook:
        return install_hook()

    rc = gate()
    if rc == 3:
        print("\nUp to date with the remote. Nothing to do.")
        return 0
    if rc:
        return rc
    if a.dry_run:
        print("\nDRY RUN: every gate passed. Nothing was pushed.")
        return 0

    step(6, f"push to {REMOTE}/{BRANCH}")
    # UCWS_PUSH_GATED tells the pre-push hook the gate has already run, so the
    # suite is not executed twice for one push.
    import os
    env = dict(os.environ, UCWS_PUSH_GATED="1")
    p = subprocess.run(["git", "push", REMOTE, BRANCH], cwd=str(REPO),
                       text=True, env=env)
    if p.returncode:
        return fail("git push failed.")

    step(7, "verify the remote actually has it")
    local = run(["git", "rev-parse", "HEAD"]).stdout.strip()
    remote = run(["git", "ls-remote", REMOTE,
                  f"refs/heads/{BRANCH}"]).stdout.split()
    remote_sha = remote[0] if remote else ""
    print(f"    local  {local[:7]}")
    print(f"    remote {remote_sha[:7]}")
    if local != remote_sha:
        return fail("the remote head does not match the local head.")
    print(f"\nPushed and verified: {local[:7]}")
    return 0


HOOK = """#!/bin/sh
# Installed by scripts/push.py. A bare `git push` is gated too, so the control
# does not depend on remembering which command to type.
if [ "$UCWS_PUSH_GATED" = "1" ]; then exit 0; fi
echo "pre-push: running the verification suite (bare git push)"
python scripts/verify_source_registry.py > /tmp/ucws_prepush.log 2>&1
rc=$?
tail -n 2 /tmp/ucws_prepush.log
if [ "$rc" = "1" ]; then
  echo ""
  echo "PUSH BLOCKED: the verification suite exited 1."
  echo "Use: python scripts/push.py   (scan, verify, push, confirm - in order)"
  exit 1
fi
echo "pre-push: suite exit $rc - allowed"
exit 0
"""


def install_hook():
    hooks = Path(run(["git", "rev-parse", "--git-path", "hooks"]
                     ).stdout.strip())
    if not hooks.is_absolute():
        hooks = REPO / hooks
    hooks.mkdir(parents=True, exist_ok=True)
    path = hooks / "pre-push"
    path.write_text(HOOK, encoding="utf-8", newline="\n")
    try:
        path.chmod(0o755)
    except OSError:
        pass
    print(f"installed {path}")
    print("A bare `git push` now runs the suite and refuses on exit 1.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
