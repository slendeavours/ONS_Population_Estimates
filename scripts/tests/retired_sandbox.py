"""Run a retired script in a sandbox that cannot reach the database.

Used by every test that executes a retired script as a subprocess. If a guard
is ever lost, the script must still fail harmlessly instead of repeating the
2026-10-11 incident (a retired script run for real by the test suite).

What the child gets:
- a copy of the script alone in a fresh temporary directory, which is also the
  child's working directory and its only sys.path entry besides the standard
  library. scripts/ is not on its path, so "import _db" fails, and the repo
  .env (which _db finds through its own file location) is never found;
- python -E -s: PYTHON* variables are ignored and no user site-packages;
- an environment built from nothing: PATH and SystemRoot (the only ones the
  interpreter needs), dummy credentials, and a database host and port that
  cannot connect (PG_HOST, PG_HOST_OVERRIDE = invalid.invalid, PG_PORT = 1).
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def scrubbed_env():
    env = {}
    for k in ("PATH", "SystemRoot"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    env.update({
        "PG_HOST": "invalid.invalid",
        "PG_HOST_OVERRIDE": "invalid.invalid",
        "PG_PORT": "1",
        "PG_DATABASE": "no_such_database",
        "PG_USER": "dummy_user",
        "PG_PASSWORD": "dummy_password",
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    return env


def run_retired(script, import_module=False, stub_modules=(), timeout=60):
    """Run (or, with import_module, import) a copy of the script in the sandbox.

    stub_modules: names of modules to plant beside the copy as files that raise
    ImportError, to prove the guard fires before any import.
    """
    script = Path(script)
    with tempfile.TemporaryDirectory() as d:
        copy = Path(d) / script.name
        copy.write_bytes(script.read_bytes())
        for mod in stub_modules:
            (Path(d) / (mod + ".py")).write_text(
                "raise ImportError('broken on purpose')\n")
        if import_module:
            cmd = [sys.executable, "-E", "-s", "-c", "import " + script.stem]
        else:
            cmd = [sys.executable, "-E", "-s", str(copy)]
        return subprocess.run(cmd, capture_output=True, text=True, cwd=d,
                              timeout=timeout, env=scrubbed_env())
