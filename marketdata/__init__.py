"""Market data (subsystem D). Quotes only, for now.

Kept out of `ledger/` and `db/` on purpose: the ledger must not learn to fetch
prices. D writes into A's existing `mark` table and nothing more -- see
docs/superpowers/specs/2026-08-05-market-data-screeners-design.md, "Marks
published into A".
"""
