# tests/test_purity.py
"""Enforces the purity boundary: `ledger/` and `importers/` may do no I/O and
must never read the wall clock. The check runs on the AST, not on raw text,
so it cannot be defeated by aliasing an import, renaming a function on
assignment, or otherwise dodging a substring scan.
"""

import ast
import pathlib

# Anchored to the repository root via __file__, NOT the process's current
# working directory. pathlib.Path("ledger").rglob("*.py") resolves relative to
# whatever directory pytest happens to be invoked from — from the repo root
# that finds 6 files, but from anywhere else (e.g. /tmp, or a CI step that cds
# first) the directory doesn't exist, rglob() on a missing directory raises
# nothing and just yields zero files, and both purity tests below pass having
# inspected exactly nothing. Proven: run from /tmp, "0 files, tests still
# pass"; run from the repo root, "6 files". __file__-relative discovery makes
# the guard's behavior independent of the caller's cwd.
_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

PURE_PACKAGES = ["ledger", "importers"]

# Importing any of these top-level modules is I/O by definition. Matched on
# the real (aliased-through) module name, so `import os as o` is still caught.
FORBIDDEN_IMPORTS = {
    "asyncpg",
    "psycopg",
    "psycopg2",
    "requests",
    "httpx",
    "aiohttp",
    "socket",
    "sqlite3",
    "os",
    "subprocess",
    "pathlib",
    "shutil",
    "urllib",
    "http",
    "time",
    "importlib",
}
# Deliberately NOT forbidden — legitimate in pure code:
# csv, io (io.StringIO is in-memory, not I/O), hashlib, datetime, decimal,
# uuid, dataclasses, enum, collections, typing, re, ast.

# Calling one of these builtins directly (as a bare name, not an attribute)
# is I/O, dynamic import, or code execution.
FORBIDDEN_CALLS = {"open", "__import__", "eval", "exec", "input", "compile"}

# Accessing one of these attributes — called or not — reads the wall clock or
# shells out. Matched on the attribute name alone (not the full dotted path),
# which is what catches `dt.now()`, `datetime.now` assigned to a variable,
# and `t.time()` regardless of how the owning module was imported or aliased.
FORBIDDEN_ATTRS = {
    "now",
    "utcnow",
    "today",
    "monotonic",
    "perf_counter",
    "time",
    "system",
    "popen",
}


def purity_violations(source: str, filename: str = "<string>") -> list[str]:
    """Return a list of human-readable violation strings for `source`.

    Empty list means clean. Each entry is tagged with its kind
    (`import:` / `call:` / `attr:`) as its first word, so callers can filter
    by category if they want to.
    """
    tree = ast.parse(source, filename=filename)
    violations: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top in FORBIDDEN_IMPORTS:
                    violations.append(
                        f"import: {filename}:{node.lineno}: forbidden import '{alias.name}'"
                    )
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                top = node.module.split(".")[0]
                if top in FORBIDDEN_IMPORTS:
                    violations.append(
                        f"import: {filename}:{node.lineno}: forbidden import 'from {node.module}'"
                    )
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in FORBIDDEN_CALLS:
                violations.append(
                    f"call: {filename}:{node.lineno}: forbidden call '{func.id}(...)'"
                )
        elif isinstance(node, ast.Attribute):
            if node.attr in FORBIDDEN_ATTRS:
                violations.append(
                    f"attr: {filename}:{node.lineno}: forbidden attribute access '.{node.attr}'"
                )

    return violations


def _python_files(pkg: str) -> list[pathlib.Path]:
    """List every .py file under a package, anchored to the repo root.

    A checker that silently scans zero files is worse than no checker at all —
    it reports green while checking nothing. Fail loudly here instead: if a
    package directory yields zero .py files, that is itself a bug in the
    guard (wrong path, missing package, cwd-relative discovery) and must
    never be allowed to look identical to "scanned everything, found nothing
    wrong."
    """
    pkg_path = _REPO_ROOT / pkg
    files = list(pkg_path.rglob("*.py"))
    assert files, (
        f"purity guard found zero .py files under {pkg_path} — it is scanning "
        "nothing, which is exactly how the cwd-relative version of this "
        "check shipped silently blind. This is a bug in the guard itself, "
        "not a clean package."
    )
    return files


def _violations_for_packages(packages: list[str], kinds: set[str]) -> list[str]:
    offenders = []
    for pkg in packages:
        for path in _python_files(pkg):
            for v in purity_violations(path.read_text(), str(path)):
                if v.split(":", 1)[0] in kinds:
                    offenders.append(v)
    return offenders


def test_pure_packages_have_no_io_imports():
    offenders = _violations_for_packages(PURE_PACKAGES, {"import"})
    assert not offenders, "I/O imports in pure package:\n" + "\n".join(offenders)


def test_pure_packages_do_not_read_the_clock():
    offenders = _violations_for_packages(PURE_PACKAGES, {"call", "attr"})
    assert not offenders, (
        "Pure code must take time as a parameter, and must not shell out or "
        "perform ad hoc I/O:\n" + "\n".join(offenders)
    )


# --- Guard self-tests -------------------------------------------------------
#
# A checker with no tests of its own is exactly how the substring-scan
# version of this file shipped without anyone noticing it was toothless.
# Each sample below is a realistic way pure code accidentally (or
# deliberately) reads the clock or performs I/O; each must be caught.

_CLOCK_VIA_ALIASED_IMPORT = "from datetime import datetime as dt\ndt.now()\n"

_CLOCK_VIA_ASSIGNED_REFERENCE = "from datetime import datetime\n_now = datetime.now\n_now()\n"

_CLOCK_VIA_MODULE_ALIAS = "import time as t\nt.time()\n"

_OPEN_CALL = 'open("f")\n'

_SUBPROCESS_CALL = "import subprocess\nsubprocess.run([])\n"

_DYNAMIC_IMPORT = "import importlib\nimportlib.import_module('asyncpg')\n"

_CLEAN_SAMPLE = (
    "from dataclasses import dataclass\n"
    "from decimal import Decimal\n"
    "\n"
    "\n"
    "@dataclass(frozen=True)\n"
    "class Money:\n"
    "    amount: Decimal\n"
    "    currency: str\n"
    "\n"
    "\n"
    "def add(a: Money, b: Money) -> Money:\n"
    "    if a.currency != b.currency:\n"
    "        raise ValueError('currency mismatch')\n"
    "    return Money(a.amount + b.amount, a.currency)\n"
)


def test_checker_catches_clock_read_via_aliased_class_import():
    assert purity_violations(_CLOCK_VIA_ALIASED_IMPORT)


def test_checker_catches_clock_read_via_assigned_reference():
    assert purity_violations(_CLOCK_VIA_ASSIGNED_REFERENCE)


def test_checker_catches_clock_read_via_module_alias():
    assert purity_violations(_CLOCK_VIA_MODULE_ALIAS)


def test_checker_catches_open_call():
    assert purity_violations(_OPEN_CALL)


def test_checker_catches_subprocess_call():
    assert purity_violations(_SUBPROCESS_CALL)


def test_checker_catches_dynamic_import():
    assert purity_violations(_DYNAMIC_IMPORT)


def test_checker_is_quiet_on_clean_source():
    assert purity_violations(_CLEAN_SAMPLE) == []


# --- Guard self-test: discovery must actually find files --------------------
#
# This is the direct regression test for the cwd-relative bug: it calls the
# same discovery helper the two purity tests above use and asserts it finds a
# non-zero count for both packages. Fails if _python_files ever goes back to
# resolving against the process cwd instead of _REPO_ROOT (run this suite
# from any directory other than the repo root and a cwd-relative version
# reports 0 for both).


def test_discovery_finds_files_in_ledger():
    files = _python_files("ledger")
    assert len(files) > 0


def test_discovery_finds_files_in_importers():
    files = _python_files("importers")
    assert len(files) > 0
