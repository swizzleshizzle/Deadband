# tests/test_purity.py
import ast
import pathlib

FORBIDDEN = {"asyncpg", "psycopg", "requests", "httpx", "aiohttp", "socket", "sqlite3"}
PURE_PACKAGES = ["ledger", "importers"]


def _imports(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


def test_pure_packages_have_no_io_imports():
    offenders = []
    for pkg in PURE_PACKAGES:
        for path in pathlib.Path(pkg).rglob("*.py"):
            bad = _imports(path) & FORBIDDEN
            if bad:
                offenders.append(f"{path}: {sorted(bad)}")
    assert not offenders, "I/O imports in pure package:\n" + "\n".join(offenders)


def test_pure_packages_do_not_read_the_clock():
    offenders = []
    for pkg in PURE_PACKAGES:
        for path in pathlib.Path(pkg).rglob("*.py"):
            src = path.read_text()
            for needle in ("datetime.now(", "datetime.utcnow(", "time.time("):
                if needle in src:
                    offenders.append(f"{path}: {needle}")
    assert not offenders, "Pure code must take time as a parameter:\n" + "\n".join(offenders)
