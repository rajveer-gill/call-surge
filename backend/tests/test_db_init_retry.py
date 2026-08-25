"""A cold database at boot must not disable the app for the life of the process.

runtime.USE_DB is decided once, in init_db(), and never revisited. Staging booted
while its free-tier Postgres was waking, the single connect timed out at 10s, and
the backend then reported "no database" for hours with the database healthy and
DATABASE_URL correctly set. The admin console said "not connected (DATABASE_URL)",
which sent the investigation after a variable that was never missing.
"""
import pytest

import database


def test_no_url_is_reported_as_such(monkeypatch, caplog):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert database.init_db() is False


def test_boot_connect_retries_before_giving_up(monkeypatch):
    """A database that is slow to wake should still end up connected."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")
    monkeypatch.setattr(database._time, "sleep", lambda _s: None)
    attempts = {"n": 0}

    class _Cur:
        def execute(self, *a, **k): pass
        def close(self): pass

    class _Conn:
        def cursor(self): return _Cur()
        def commit(self): pass
        def close(self): pass

    import psycopg2

    def _connect(*a, **k):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise psycopg2.OperationalError("timeout expired")
        return _Conn()

    monkeypatch.setattr(psycopg2, "connect", _connect)
    database.init_db()
    # The point: it did not give up on the first slow connect.
    assert attempts["n"] == 3


def test_gives_up_eventually_rather_than_hanging_boot(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")
    monkeypatch.setattr(database._time, "sleep", lambda _s: None)
    attempts = {"n": 0}
    import psycopg2

    def _always_fail(*a, **k):
        attempts["n"] += 1
        raise psycopg2.OperationalError("timeout expired")

    monkeypatch.setattr(psycopg2, "connect", _always_fail)
    assert database.init_db() is False
    assert attempts["n"] == 4
