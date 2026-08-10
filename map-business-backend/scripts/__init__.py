"""Operational scripts package (R2-P2-04).

Makes every script runnable in BOTH documented forms from the project
root on a clean checkout:

    uv run python -m scripts.verify_audit_chain      # package form
    uv run python scripts/verify_audit_chain.py      # direct form

The package form needs this ``__init__.py``; the direct form relies on the
per-script sys.path bootstrap (see each script's header).
"""
