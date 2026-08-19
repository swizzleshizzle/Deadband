"""Every real export parses with zero blocking rows (branch B acceptance,
gap #31's closure criterion).

Reads imports/ (gitignored; present only on the owner's machines) behind a
skip guard. Asserts COUNTS ONLY -- a failure message must never carry row
text, amounts, or account refs from the real files."""

import pathlib

import pytest

from importers.fidelity import FidelityImporter

IMPORTS = pathlib.Path(__file__).resolve().parents[1] / "imports"

pytestmark = pytest.mark.skipif(
    not IMPORTS.exists(), reason="imports/ not present; real-file acceptance is owner-local"
)


def test_every_real_export_parses_with_zero_blocking_rows():
    files = sorted(IMPORTS.glob("*.csv"))
    assert files, "imports/ exists but holds no csv files"
    blocked = {}
    for path in files:
        batch = FidelityImporter().parse(path.read_text(encoding="utf-8"))
        if batch.blocking:
            blocked[path.name] = len(batch.blocking)
    assert blocked == {}, f"files with blocking rows (name: count only): {blocked}"
