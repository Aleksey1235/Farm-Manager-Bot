from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

import pytest

from farmbot.calc import percent_change, latest_report_week_start, week_bounds
from farmbot.db import Database


def test_percent_change():
    assert percent_change(120, 100) == 20.0
    assert percent_change(80, 100) == -20.0
    assert percent_change(0, 0) == 0.0
    assert percent_change(10, 0) is None


def test_latest_report_week_start_before_and_after_schedule():
    tz = ZoneInfo("UTC")
    # Sunday 22:00, report scheduled Sunday 23:00 -> previous week
    now = datetime(2026, 8, 23, 22, 0, tzinfo=tz)
    assert latest_report_week_start(now, 6, 23, 0) == date(2026, 8, 10)

    # Sunday 23:01 -> current week
    now2 = datetime(2026, 8, 23, 23, 1, tzinfo=tz)
    assert latest_report_week_start(now2, 6, 23, 0) == date(2026, 8, 17)


@pytest.mark.asyncio
async def test_my_week_range_inactive_day_is_not_unresolved(tmp_path):
    tz = ZoneInfo("UTC")
    db = Database(tmp_path / "v23.sqlite3")
    await db.init()

    uid = 100
    now = datetime(2026, 8, 23, 20, 0, tzinfo=tz)
    await db.ensure_member(uid, "Inactive", now)

    wb = week_bounds(now.date())
    s = await db.member_week_summary_for_range(uid, wb.start, wb.end, tz, now)

    assert s["unresolved_finished_days"] == 0
    assert s["gross"] == 0
    assert s["due"] == 0
    assert all(d["status"] == "inactive" for d in s["days"])


@pytest.mark.asyncio
async def test_department_snapshot_and_week_comparison_data(tmp_path):
    tz = ZoneInfo("UTC")
    db = Database(tmp_path / "snapshot.sqlite3")
    await db.init()
    await db.set_setting("content_discount", 50000)

    uid = 101
    start = datetime(2026, 8, 17, 10, 0, tzinfo=tz)
    await db.ensure_member(uid, "Farmer", start)

    # Monday: exactly 4 hours -> auto 100k.
    await db.start_farm(uid, start)
    await db.finish_farm(uid, datetime(2026, 8, 17, 14, 0, tzinfo=tz), tz)

    snap = await db.department_snapshot(
        date(2026, 8, 17), date(2026, 8, 23), tz,
        datetime(2026, 8, 23, 23, 0, tzinfo=tz)
    )

    assert snap["members"] == 1
    assert snap["active_members"] == 1
    assert snap["total_seconds"] == 4 * 3600
    assert snap["gross"] == 100000
    assert snap["ready_due"] == 100000
    assert snap["unpaid"] == 1


@pytest.mark.asyncio
async def test_payment_overview_categories(tmp_path):
    tz = ZoneInfo("UTC")
    db = Database(tmp_path / "payments.sqlite3")
    await db.init()

    paid_uid = 201
    pending_uid = 202
    unpaid_uid = 203
    now = datetime(2026, 8, 23, 20, 0, tzinfo=tz)

    for uid, name in [(paid_uid, "Paid"), (pending_uid, "Pending"), (unpaid_uid, "Unpaid")]:
        await db.ensure_member(uid, name, now)
        st = datetime(2026, 8, 17, 10, 0, tzinfo=tz)
        await db.start_farm(uid, st)
        await db.finish_farm(uid, datetime(2026, 8, 17, 14, 0, tzinfo=tz), tz)

    week_start = date(2026, 8, 17)
    week_end = date(2026, 8, 23)

    await db.set_payment_proof(paid_uid, week_start.isoformat(), 100000, "https://x/paid.png")
    await db.review_payment(paid_uid, week_start.isoformat(), True, 999, now)

    await db.set_payment_proof(pending_uid, week_start.isoformat(), 100000, "https://x/pending.png")

    ov = await db.payment_overview(week_start, week_end, tz, now)

    assert [x["user_id"] for x in ov["paid"]] == [paid_uid]
    assert [x["user_id"] for x in ov["pending"]] == [pending_uid]
    assert [x["user_id"] for x in ov["unpaid"]] == [unpaid_uid]


@pytest.mark.asyncio
async def test_weekly_report_run_is_idempotent(tmp_path):
    tz = ZoneInfo("UTC")
    db = Database(tmp_path / "report.sqlite3")
    await db.init()
    ws = date(2026, 8, 17)
    assert not await db.report_was_sent(ws)

    now = datetime(2026, 8, 23, 23, 0, tzinfo=tz)
    await db.mark_report_sent(ws, now, 12345)
    assert await db.report_was_sent(ws)

    # Updating same week remains one logical record and still "sent".
    await db.mark_report_sent(ws, now + timedelta(minutes=1), 12346)
    assert await db.report_was_sent(ws)


@pytest.mark.asyncio
async def test_session_reminder_only_once_per_segment(tmp_path):
    tz = ZoneInfo("UTC")
    db = Database(tmp_path / "reminder.sqlite3")
    await db.init()

    uid = 300
    now = datetime(2026, 8, 23, 10, 0, tzinfo=tz)
    await db.ensure_member(uid, "Reminder", now)
    await db.start_farm(uid, now)
    segment = now.isoformat()

    created1 = await db.create_session_reminder(uid, segment, now + timedelta(hours=6))
    created2 = await db.create_session_reminder(uid, segment, now + timedelta(hours=6, minutes=1))

    assert created1 is True
    assert created2 is False
    assert await db.session_reminder_exists(uid, segment)

    pending = await db.unresolved_session_reminders()
    assert len(pending) == 1

    await db.resolve_session_reminder(uid, segment, "continue", now + timedelta(hours=6, minutes=2))
    pending2 = await db.unresolved_session_reminders()
    assert pending2 == []


@pytest.mark.asyncio
async def test_daily_report_notification_saved_once(tmp_path):
    tz = ZoneInfo("UTC")
    db = Database(tmp_path / "notification.sqlite3")
    await db.init()
    uid = 401
    now = datetime(2026, 8, 23, 10, 0, tzinfo=tz)
    await db.ensure_member(uid, "Notify", now)

    assert await db.daily_report_notification(uid, "2026-08-23") is None
    await db.save_daily_report_notification(uid, "2026-08-23", 10, 20)
    row = await db.daily_report_notification(uid, "2026-08-23")
    assert row == {"channel_id": 10, "message_id": 20}

    # Same user/day updates the one record rather than creating duplicates.
    await db.save_daily_report_notification(uid, "2026-08-23", 11, 21)
    row2 = await db.daily_report_notification(uid, "2026-08-23")
    assert row2 == {"channel_id": 11, "message_id": 21}


@pytest.mark.asyncio
async def test_sync_active_members_marks_departed_inactive(tmp_path):
    tz = ZoneInfo("UTC")
    db = Database(tmp_path / "roster.sqlite3")
    await db.init()
    now = datetime(2026, 8, 23, 10, 0, tzinfo=tz)
    await db.ensure_member(501, "A", now)
    await db.ensure_member(502, "B", now)

    await db.sync_active_members({501})
    async with db.connect() as conn:
        rows = await (await conn.execute(
            "SELECT user_id,active FROM members ORDER BY user_id"
        )).fetchall()
    assert [(r["user_id"], r["active"]) for r in rows] == [(501, 1), (502, 0)]


@pytest.mark.asyncio
async def test_historical_week_keeps_departed_member_with_activity(tmp_path):
    tz = ZoneInfo("UTC")
    db = Database(tmp_path / "history.sqlite3")
    await db.init()
    uid = 601
    start = datetime(2026, 8, 10, 10, 0, tzinfo=tz)
    await db.ensure_member(uid, "Former", start)
    await db.start_farm(uid, start)
    await db.finish_farm(uid, datetime(2026, 8, 10, 14, 0, tzinfo=tz), tz)

    # Member later leaves FARM.
    await db.sync_active_members(set())

    rows = await db.department_week_stats_for_range(
        date(2026, 8, 10), date(2026, 8, 16), tz,
        datetime(2026, 8, 23, 20, 0, tzinfo=tz)
    )
    assert [r["user_id"] for r in rows] == [uid]
    assert rows[0]["total_seconds"] == 4 * 3600
