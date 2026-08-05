# tests/test_purity.py
"""Enforces the purity boundary: `ledger/` and `importers/` may do no I/O and
must never read the wall clock. The check runs on the AST, not on raw text,
so it cannot be defeated by aliasing an import, renaming a function on
assignment, or otherwise dodging a substring scan.
"""

import ast
import pathlib

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


def _violations_for_packages(packages: list[str], kinds: set[str]) -> list[str]:
    offenders = []
    for pkg in packages:
        for path in pathlib.Path(pkg).rglob("*.py"):
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
