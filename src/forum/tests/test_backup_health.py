"""Contract tests: backup age on the forum's /health (engram-alpha#3276, part B).

Written BEFORE the implementation (contract-first, #3045).

Contract (designer: ariadne-calvino, accepted: kepler-somnium, 2026-10-01):
  B4  app configured with a backup dir -> /health gains
      "backup": {"last_success_at": ..., "age_hours": <float>} and
      deps.backup = "ok" if age <= 36h else "degraded".
  B5  missing / unreadable stamp -> deps.backup = "degraded",
      last_success_at: null. Never "ok".
  B6  NOT configured -> no "backup" key and no deps.backup (existing tests
      stay green unchanged).
  B7  HTTP status stays 200 in every case (the installer's ``curl --fail``).

CONFIGURATION API ASSUMED HERE (the only guess; one place to adjust):
``create_app(..., backup_dir=<path>)``. ``main()`` is expected to feed it from
``$FORUM_BACKUP_DIR``. If the implementation names the knob differently, change
``_make_app`` below and nothing else.

The stamp format is the one written by tools/forum_backup.py (part A):
``<backup-dir>/.last-success.json`` = {"at": <UTC ISO>, "commit": ..., "committed": ...}.
Time is controlled by writing the stamp's ``at`` relative to now -- no sleeping.
The margin between 35h/37h and the 36h threshold dwarfs test runtime.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# src/forum/tests/ -> src (so ``import forum`` resolves), as in the sibling tests.
_SRC = Path(__file__).resolve().parent.parent.parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from forum.db import init_db  # noqa: E402
from forum.server import create_app  # noqa: E402

STAMP = ".last-success.json"


def _make_app(tmp_path: Path, backup_dir: Path | None, dep_results: dict | None = None):
    db_path = str(tmp_path / "forum.db")
    conn = sqlite3.connect(db_path)
    init_db(conn)
    conn.close()
    kwargs = {}
    if backup_dir is not None:
        kwargs["backup_dir"] = str(backup_dir)  # <- the assumed knob
    if dep_results is not None:
        kwargs["dep_results"] = dep_results
    app = create_app(db_path, str(tmp_path / "audit.jsonl"), **kwargs)
    app.config["TESTING"] = True
    return app


def _write_stamp(backup_dir: Path, age: timedelta, fmt: str = "iso") -> str:
    backup_dir.mkdir(parents=True, exist_ok=True)
    at = datetime.now(timezone.utc) - age
    at_s = at.strftime("%Y-%m-%dT%H:%M:%SZ") if fmt == "z" else at.isoformat()
    (backup_dir / STAMP).write_text(
        json.dumps({"at": at_s, "commit": "abc123", "committed": True})
    )
    return at_s


@pytest.fixture
def backup_dir(tmp_path):
    d = tmp_path / "backup"
    d.mkdir()
    return d


def _health(app):
    resp = app.test_client().get("/health")
    return resp, resp.get_json()


# ---------------------------------------------------------------------------
# B4 -- age threshold at 36h
# ---------------------------------------------------------------------------

class TestBackupAgeThreshold:

    def test_ok_at_35h(self, tmp_path, backup_dir):
        _write_stamp(backup_dir, timedelta(hours=35))
        resp, data = _health(_make_app(tmp_path, backup_dir))
        assert resp.status_code == 200
        assert data["deps"]["backup"] == "ok"
        assert data["backup"]["age_hours"] == pytest.approx(35.0, abs=0.1)
        assert data["backup"]["last_success_at"]

    def test_degraded_at_37h(self, tmp_path, backup_dir):
        _write_stamp(backup_dir, timedelta(hours=37))
        resp, data = _health(_make_app(tmp_path, backup_dir))
        assert resp.status_code == 200
        assert data["deps"]["backup"] == "degraded"
        assert data["backup"]["age_hours"] == pytest.approx(37.0, abs=0.1)

    def test_fresh_stamp_ok_and_age_near_zero(self, tmp_path, backup_dir):
        _write_stamp(backup_dir, timedelta(minutes=1))
        _, data = _health(_make_app(tmp_path, backup_dir))
        assert data["deps"]["backup"] == "ok"
        assert 0 <= data["backup"]["age_hours"] < 0.5

    def test_z_suffix_timestamp_is_parsed(self, tmp_path, backup_dir):
        """The tool may write the 'Z' form (like its commit-message ts)."""
        _write_stamp(backup_dir, timedelta(hours=35), fmt="z")
        _, data = _health(_make_app(tmp_path, backup_dir))
        assert data["deps"]["backup"] == "ok"
        assert data["backup"]["age_hours"] == pytest.approx(35.0, abs=0.1)

    def test_age_is_computed_per_request_not_at_boot(self, tmp_path, backup_dir):
        """The same running app must re-read the stamp on every /health call:
        a daemon up for days must not report the age it had at boot."""
        _write_stamp(backup_dir, timedelta(hours=1))
        app = _make_app(tmp_path, backup_dir)
        _, first = _health(app)
        assert first["deps"]["backup"] == "ok"

        _write_stamp(backup_dir, timedelta(hours=40))  # time "passes" / backup stalls
        _, second = _health(app)
        assert second["deps"]["backup"] == "degraded"
        assert second["backup"]["age_hours"] == pytest.approx(40.0, abs=0.1)

        _write_stamp(backup_dir, timedelta(minutes=2))  # backup recovers
        _, third = _health(app)
        assert third["deps"]["backup"] == "ok"

    def test_degraded_backup_degrades_overall_status(self, tmp_path, backup_dir):
        """/health's existing rule: overall 'degraded' if any dep is not 'ok'."""
        _write_stamp(backup_dir, timedelta(hours=50))
        _, data = _health(
            _make_app(tmp_path, backup_dir, dep_results={"db": "ok", "audit_log": "ok"})
        )
        assert data["deps"]["db"] == "ok"
        assert data["deps"]["backup"] == "degraded"
        assert data["status"] == "degraded"

    def test_other_deps_preserved_next_to_backup(self, tmp_path, backup_dir):
        _write_stamp(backup_dir, timedelta(hours=1))
        _, data = _health(
            _make_app(tmp_path, backup_dir, dep_results={"db": "ok", "embeddings": "ok"})
        )
        assert data["deps"] == {"db": "ok", "embeddings": "ok", "backup": "ok"}
        assert data["status"] == "ok"


# ---------------------------------------------------------------------------
# B5 -- missing / unreadable stamp is never "ok"
# ---------------------------------------------------------------------------

class TestMissingOrUnreadableStamp:

    def test_missing_stamp_is_degraded(self, tmp_path, backup_dir):
        resp, data = _health(_make_app(tmp_path, backup_dir))
        assert resp.status_code == 200
        assert data["deps"]["backup"] == "degraded"
        assert data["backup"]["last_success_at"] is None

    @pytest.mark.parametrize(
        "content",
        ["not json at all {{{", "", "[]", '{"at": 12345}', '{"at": "yesterday-ish"}', '{"commit": "x"}'],
    )
    def test_garbage_stamp_is_degraded(self, tmp_path, backup_dir, content):
        (backup_dir / STAMP).write_text(content)
        resp, data = _health(_make_app(tmp_path, backup_dir))
        assert resp.status_code == 200
        assert data["deps"]["backup"] == "degraded"
        assert data["backup"]["last_success_at"] is None

    def test_future_stamp_beyond_clock_skew_is_degraded(self, tmp_path, backup_dir):
        """A stamp dated in the future gives a negative age; without a guard it
        compares <= 36h and reads ok forever. Beyond 1h of clock-skew slack it is
        treated like an unreadable stamp (last_success_at null, degraded)."""
        _write_stamp(backup_dir, timedelta(hours=-5))
        resp, data = _health(_make_app(tmp_path, backup_dir))
        assert resp.status_code == 200
        assert data["deps"]["backup"] == "degraded"
        assert data["backup"]["last_success_at"] is None
        assert data["backup"]["age_hours"] is None

    def test_slightly_future_stamp_within_skew_slack_is_ok_and_age_not_negative(
        self, tmp_path, backup_dir
    ):
        _write_stamp(backup_dir, timedelta(minutes=-10))
        _, data = _health(_make_app(tmp_path, backup_dir))
        assert data["deps"]["backup"] == "ok"
        assert data["backup"]["age_hours"] == 0.0

    def test_nonexistent_backup_dir_is_degraded_not_error(self, tmp_path):
        resp, data = _health(_make_app(tmp_path, tmp_path / "no-such-dir"))
        assert resp.status_code == 200
        assert data["deps"]["backup"] == "degraded"
        assert data["backup"]["last_success_at"] is None


# ---------------------------------------------------------------------------
# B6 -- not configured: no backup key at all (these are GREEN on dev today)
# ---------------------------------------------------------------------------

class TestUnconfigured:

    def test_no_backup_key_when_unconfigured(self, tmp_path):
        resp, data = _health(_make_app(tmp_path, None))
        assert resp.status_code == 200
        assert "backup" not in data

    def test_no_deps_backup_when_unconfigured_with_deps(self, tmp_path):
        _, data = _health(_make_app(tmp_path, None, dep_results={"db": "ok"}))
        assert data["deps"] == {"db": "ok"}
        assert data["status"] == "ok"

    def test_unconfigured_ignores_a_stamp_lying_around(self, tmp_path, backup_dir):
        """Configuration, not file discovery, turns the feature on."""
        _write_stamp(backup_dir, timedelta(hours=99))
        _, data = _health(_make_app(tmp_path, None))
        assert "backup" not in data
        assert data == {"status": "ok"}


# ---------------------------------------------------------------------------
# B7 -- HTTP 200 in every case (installer uses ``curl --fail``)
# ---------------------------------------------------------------------------

class TestAlways200:

    @pytest.mark.parametrize("age_h", [1, 35, 37, 24 * 12])
    def test_200_for_any_age(self, tmp_path, backup_dir, age_h):
        _write_stamp(backup_dir, timedelta(hours=age_h))
        resp, data = _health(_make_app(tmp_path, backup_dir))
        assert resp.status_code == 200
        assert "backup" in data, "configured app must report the backup block"

    def test_200_when_unconfigured(self, tmp_path):
        resp, _ = _health(_make_app(tmp_path, None))
        assert resp.status_code == 200
