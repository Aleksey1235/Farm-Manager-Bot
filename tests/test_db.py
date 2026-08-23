from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from farmbot.db import Database


@pytest.mark.asyncio
async def test_farm_content_pause_resume_does_not_count_content(tmp_path):
    tz = ZoneInfo("UTC")
    db = Database(tmp_path / "t.sqlite3")
    await db.init()
    uid = 1
    await db.ensure_member(uid, "Tester", datetime(2026,8,22,10,0,tzinfo=tz))
    await db.start_farm(uid, datetime(2026,8,22,10,0,tzinfo=tz))
    await db.pause_for_content(uid, datetime(2026,8,22,11,0,tzinfo=tz))
    await db.return_from_content(uid, datetime(2026,8,22,12,0,tzinfo=tz))
    result = await db.finish_farm(uid, datetime(2026,8,22,15,0,tzinfo=tz), tz)
    assert result["seconds"] == 4*3600
    assert result["status"] == "auto"
    assert result["amount"] == 100000


@pytest.mark.asyncio
async def test_manual_amount_is_superseded_after_reaching_threshold(tmp_path):
    tz = ZoneInfo("UTC")
    db = Database(tmp_path / "t.sqlite3")
    await db.init()
    uid = 2
    t0 = datetime(2026,8,22,10,0,tzinfo=tz)
    await db.ensure_member(uid, "Tester2", t0)
    await db.start_farm(uid, t0)
    r1 = await db.finish_farm(uid, datetime(2026,8,22,12,0,tzinfo=tz), tz)
    assert r1["status"] == "pending"
    await db.assign_manual_amount(uid, "2026-08-22", 300000, 99, datetime(2026,8,22,12,5,tzinfo=tz))
    await db.start_farm(uid, datetime(2026,8,22,13,0,tzinfo=tz))
    r2 = await db.finish_farm(uid, datetime(2026,8,22,15,0,tzinfo=tz), tz)
    assert r2["status"] == "auto"
    assert r2["amount"] == 100000


@pytest.mark.asyncio
async def test_never_farmed_day_is_not_pending_and_does_not_block_week(tmp_path):
    tz = ZoneInfo("UTC")
    db = Database(tmp_path / "inactive.sqlite3")
    await db.init()
    uid = 3
    now = datetime(2026, 8, 22, 20, 0, tzinfo=tz)
    await db.ensure_member(uid, "Inactive", now)
    summary = await db.member_week_summary(uid, now.date(), tz, now)
    today = next(x for x in summary["days"] if x["day"] == now.date())
    assert today["status"] == "inactive"
    assert today["counts_for_due"] is False
    assert summary["unresolved_finished_days"] == 0
    assert summary["due"] == 0


@pytest.mark.asyncio
async def test_finished_under_threshold_is_pending_and_blocks_week(tmp_path):
    tz = ZoneInfo("UTC")
    db = Database(tmp_path / "pending.sqlite3")
    await db.init()
    uid = 4
    start = datetime(2026, 8, 22, 10, 0, tzinfo=tz)
    await db.ensure_member(uid, "Pending", start)
    await db.start_farm(uid, start)
    await db.finish_farm(uid, datetime(2026, 8, 22, 12, 0, tzinfo=tz), tz)
    summary = await db.member_week_summary(uid, start.date(), tz, datetime(2026, 8, 22, 13, 0, tzinfo=tz))
    today = next(x for x in summary["days"] if x["day"] == start.date())
    assert today["status"] == "pending"
    assert summary["unresolved_finished_days"] == 1
    assert summary["due"] is None


@pytest.mark.asyncio
async def test_manual_amount_assignment_updates_pending_report(tmp_path):
    tz = ZoneInfo("UTC")
    db = Database(tmp_path / "manual_card.sqlite3")
    await db.init()

    uid = 20
    start = datetime(2026, 8, 22, 10, 0, tzinfo=tz)
    await db.ensure_member(uid, "CardUser", start)
    await db.start_farm(uid, start)
    await db.finish_farm(uid, datetime(2026, 8, 22, 12, 30, tzinfo=tz), tz)

    pending = await db.pending_daily_reports()
    assert any(r["user_id"] == uid and r["day"] == "2026-08-22" for r in pending)

    await db.assign_manual_amount(
        uid, "2026-08-22", 275000, 999,
        datetime(2026, 8, 22, 12, 35, tzinfo=tz)
    )

    pending = await db.pending_daily_reports()
    assert not any(r["user_id"] == uid and r["day"] == "2026-08-22" for r in pending)

    summary = await db.member_week_summary(
        uid,
        start.date(),
        tz,
        datetime(2026, 8, 22, 13, 0, tzinfo=tz)
    )
    today = next(x for x in summary["days"] if x["day"] == start.date())
    assert today["status"] == "manual"
    assert today["amount"] == 275000
