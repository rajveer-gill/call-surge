"""An owner must never be treated as less than a manager.

Adding `owner` above `manager` broke six places that asked `role == "manager"`.
Equality was correct while manager was the top role and silently wrong the moment
it was not. The worst was deps._enforce_org_write_role, which gates every write in
the product: the head account became read-only everywhere.

These tests assert the behaviour. The last one asserts the shape, because the
behaviour tests only cover the call sites someone remembered to write a test for,
and the whole failure was about the ones nobody remembered.
"""
import re
from pathlib import Path

import pytest
from fastapi import HTTPException

import database
import deps
import routers.org as org

BACKEND = Path(__file__).resolve().parent.parent


class _Req:
    method = "POST"
    headers: dict = {}
    client = None
    url = type("U", (), {"path": "/api/org/x"})()


@pytest.mark.parametrize("role", ["manager", "owner"])
def test_write_gate_admits_manager_and_owner(role, monkeypatch):
    """The severe one: this gates every write in the app."""
    monkeypatch.setattr(deps, "audit_log", lambda *a, **k: None)
    deps._enforce_org_write_role(_Req(), role, "u1", "store-1")  # must not raise


def test_write_gate_still_blocks_a_viewer(monkeypatch):
    monkeypatch.setattr(deps, "audit_log", lambda *a, **k: None)
    with pytest.raises(HTTPException) as ei:
        deps._enforce_org_write_role(_Req(), "viewer", "u1", "store-1")
    assert ei.value.status_code == 403


@pytest.mark.parametrize("role", ["manager", "owner"])
def test_can_edit_any_is_true_for_manager_and_owner(role):
    """Drives the "Your stores" link. An owner seeing it hidden is how this was found."""
    assert database.org_role_at_least(role, "manager")


def test_can_edit_any_is_false_for_a_viewer():
    assert not database.org_role_at_least("viewer", "manager")


@pytest.mark.parametrize("role", ["manager", "owner"])
def test_store_management_admits_manager_and_owner(role, monkeypatch):
    monkeypatch.setattr(database, "db_org_memberships_org_wide",
                        lambda _u: [{"org_id": "o1", "role": role}])
    assert org._require_org_manager("u1")


def test_store_management_refuses_a_viewer(monkeypatch):
    monkeypatch.setattr(database, "db_org_memberships_org_wide",
                        lambda _u: [{"org_id": "o1", "role": "viewer"}])
    with pytest.raises(HTTPException) as ei:
        org._require_org_manager("u1")
    assert ei.value.status_code == 403


def test_no_role_equality_comparisons_remain():
    """The shape test.

    Comparing a role with == is right until a stronger role exists, then it is a
    silent privilege bug — the code keeps working and quietly excludes the account
    that matters most. Use database.org_role_at_least instead.

    A genuine value check — "is the role the caller REQUESTED equal to owner" —
    annotates itself with `# role-value-check`. Asking which value was requested is
    not the same as asking whether someone outranks someone else, and the guard
    should not push people to disguise the first as the second.

    SQL is exempt for the same reason: a WHERE clause selecting rows that ARE
    managers, or setting a role, names a value rather than ranking one.
    """
    offenders = []
    pattern = re.compile(r'(?<![\w.])role\w*\s*(==|!=)\s*[\'"](?:manager|owner|viewer)[\'"]')
    for path in list(BACKEND.glob("*.py")) + list((BACKEND / "routers").glob("*.py")):
        if path.name.startswith("test_"):
            continue
        for i, line in enumerate(path.read_text(encoding="utf-8").split("\n"), 1):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith("*"):
                continue
            if "role-value-check" in line:
                continue
            if pattern.search(line):
                offenders.append(f"{path.name}:{i} {stripped[:90]}")
    assert offenders == [], (
        "compare roles by rank, not equality — use database.org_role_at_least:\n"
        + "\n".join(offenders)
    )
