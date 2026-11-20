"""A test that needs the workbench of the person who runs it, next to the checkout.

``test_the_workbench_inventory_and_the_package_cannot_DRIFT`` reads ``scripts/inventario_voce6.py`` three directories above this file, where a
developer's workbench is; a clean clone of the repository (the CI job) has no such folder, so the test skips, and a skip is red in the whole unit suite
of the candidate. It lives in its own file, out of ``tests/test_juggler_transport.py`` (which holds what runs on every machine), and is a file of the
environment of ``tests/test_the_unit_suite_is_accounted_for.py``.
"""
from __future__ import annotations

import pathlib

import pytest


def test_the_workbench_inventory_and_the_package_cannot_DRIFT():
    """⛔ There is one perimeter and it lives in the shipped package. The
    workbench tool that counts remaining work reads it from there; if it kept
    its own copy the two would disagree the first time one was edited - and the
    one that matters, the one that REFUSES, would be the one nobody updated."""
    from invisible_playwright._juggler import perimeter
    tool = pathlib.Path(__file__).resolve().parents[3] / "scripts" \
        / "inventario_voce6.py"
    if not tool.exists():
        pytest.skip("the workbench is not next to this checkout")
    source = tool.read_text(encoding="utf-8", errors="replace")
    assert "_fuori_dal_pacchetto" in source, (
        "the inventory no longer reads the package: it has grown its own copy "
        "of the perimeter")
    assert "perimeter.py" in source
    assert len(perimeter.OUTSIDE) > 50
