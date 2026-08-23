from datetime import datetime, date
from zoneinfo import ZoneInfo

import pytest

from farmbot.db import Database


@pytest.mark.asyncio
async def test_farm_crossing_midnight_stays_on_start_day(tmp_path):
    tz = ZoneInfo('Europe/Moscow')
    db = Database(tmp_path / 'midnight.sqlite3')
    await db.init()
    uid = 9001
    start = datetime(2026, 8, 23, 23, 0, tzinfo=tz)
    end = datetime(2026, 8, 24, 3, 0, tzinfo=tz)
    await db.ensure_member(uid, 'NightFarmer', start)
    await db.start_farm(uid, start)
    result = await db.finish_farm(uid, end, tz)
    assert result['work_day'] == '2026-08-23'
    assert result['seconds'] == 4 * 3600
    assert result['status'] == 'auto'
    assert await db.farm_seconds_for_day(uid, date(2026, 8, 23), tz, end) == 4 * 3600
    assert await db.farm_seconds_for_day(uid, date(2026, 8, 24), tz, end) == 0


@pytest.mark.asyncio
async def test_content_crossing_midnight_keeps_original_work_day(tmp_path):
    tz = ZoneInfo('Europe/Moscow')
    db = Database(tmp_path / 'content_midnight.sqlite3')
    await db.init()
    uid = 9002
    start = datetime(2026, 8, 23, 22, 30, tzinfo=tz)
    content_start = datetime(2026, 8, 23, 23, 30, tzinfo=tz)
    content_end = datetime(2026, 8, 24, 0, 30, tzinfo=tz)
    finish = datetime(2026, 8, 24, 3, 30, tzinfo=tz)
    await db.ensure_member(uid, 'ContentNight', start)
    await db.start_farm(uid, start)
    await db.pause_for_content(uid, content_start)
    await db.return_from_content(uid, content_end)
    result = await db.finish_farm(uid, finish, tz)
    assert result['work_day'] == '2026-08-23'
    assert result['seconds'] == 4 * 3600
    assert await db.farm_seconds_for_day(uid, date(2026, 8, 24), tz, finish) == 0


@pytest.mark.asyncio
async def test_sunday_to_monday_stays_in_old_week(tmp_path):
    tz = ZoneInfo('Europe/Moscow')
    db = Database(tmp_path / 'week_boundary.sqlite3')
    await db.init()
    uid = 9003
    start = datetime(2026, 8, 23, 23, 0, tzinfo=tz)
    finish = datetime(2026, 8, 24, 3, 0, tzinfo=tz)
    await db.ensure_member(uid, 'WeekBoundary', start)
    await db.start_farm(uid, start)
    await db.finish_farm(uid, finish, tz)
    old_week = await db.member_week_summary_for_range(
        uid, date(2026, 8, 17), date(2026, 8, 23), tz, finish,
        current_day=date(2026, 8, 24)
    )
    new_week = await db.member_week_summary_for_range(
        uid, date(2026, 8, 24), date(2026, 8, 30), tz, finish,
        current_day=date(2026, 8, 24)
    )
    assert old_week['total_seconds'] == 4 * 3600
    assert old_week['gross'] == 100000
    assert new_week['total_seconds'] == 0
    assert new_week['gross'] == 0


@pytest.mark.asyncio
async def test_new_session_after_finish_uses_new_calendar_day(tmp_path):
    tz = ZoneInfo('Europe/Moscow')
    db = Database(tmp_path / 'new_session.sqlite3')
    await db.init()
    uid = 9004
    first_start = datetime(2026, 8, 23, 23, 0, tzinfo=tz)
    first_finish = datetime(2026, 8, 24, 1, 0, tzinfo=tz)
    second_start = datetime(2026, 8, 24, 1, 30, tzinfo=tz)
    second_finish = datetime(2026, 8, 24, 3, 30, tzinfo=tz)
    await db.ensure_member(uid, 'TwoDays', first_start)
    await db.start_farm(uid, first_start)
    r1 = await db.finish_farm(uid, first_finish, tz)
    await db.assign_manual_amount(uid, '2026-08-23', 250000, 999, datetime(2026, 8, 24, 1, 5, tzinfo=tz))
    await db.start_farm(uid, second_start)
    r2 = await db.finish_farm(uid, second_finish, tz)
    assert r1['work_day'] == '2026-08-23'
    assert r2['work_day'] == '2026-08-24'
    assert await db.farm_seconds_for_day(uid, date(2026, 8, 23), tz, second_finish) == 2 * 3600
    assert await db.farm_seconds_for_day(uid, date(2026, 8, 24), tz, second_finish) == 2 * 3600

@pytest.mark.asyncio
async def test_active_state_exposes_original_work_day_after_midnight(tmp_path):
    tz = ZoneInfo('Europe/Moscow')
    db = Database(tmp_path / 'active_workday.sqlite3')
    await db.init()
    uid = 9005
    start = datetime(2026, 8, 23, 23, 0, tzinfo=tz)
    after_midnight = datetime(2026, 8, 24, 1, 0, tzinfo=tz)
    await db.ensure_member(uid, 'StillWorking', start)
    await db.start_farm(uid, start)
    rows = await db.active_states()
    row = next(r for r in rows if r['user_id'] == uid)
    assert row['work_day'] == '2026-08-23'
    assert await db.farm_seconds_for_day(uid, date(2026, 8, 23), tz, after_midnight) == 2 * 3600
    assert await db.farm_seconds_for_day(uid, date(2026, 8, 24), tz, after_midnight) == 0

@pytest.mark.asyncio
async def test_v23_database_migrates_work_day_columns_and_backfills(tmp_path):
    import sqlite3

    path = tmp_path / 'old_v23.sqlite3'
    con = sqlite3.connect(path)
    con.executescript('''
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE members (
            user_id INTEGER PRIMARY KEY,
            display_name TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            joined_at TEXT NOT NULL
        );
        CREATE TABLE states (
            user_id INTEGER PRIMARY KEY,
            state TEXT NOT NULL,
            farming_started_at TEXT,
            content_started_at TEXT,
            content_return_state TEXT,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE farm_segments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            end_reason TEXT
        );
    ''')
    start = '2026-08-23T23:00:00+03:00'
    con.execute("INSERT INTO members VALUES(?,?,?,?,?)".replace(',?,?,?,?', ',?,?,?'), (9100, 'Legacy', 1, start))
    con.execute("INSERT INTO states(user_id,state,farming_started_at,updated_at) VALUES(?,?,?,?)", (9100, 'farming', start, start))
    con.execute("INSERT INTO farm_segments(user_id,started_at) VALUES(?,?)", (9100, start))
    con.commit()
    con.close()

    db = Database(path)
    await db.init()

    async with db.connect() as conn:
        state_cols = {r['name'] for r in await (await conn.execute('PRAGMA table_info(states)')).fetchall()}
        seg_cols = {r['name'] for r in await (await conn.execute('PRAGMA table_info(farm_segments)')).fetchall()}
        state = await (await conn.execute('SELECT work_day FROM states WHERE user_id=9100')).fetchone()
        seg = await (await conn.execute('SELECT work_day FROM farm_segments WHERE user_id=9100')).fetchone()

    assert 'work_day' in state_cols
    assert 'work_day' in seg_cols
    assert state['work_day'] == '2026-08-23'
    assert seg['work_day'] == '2026-08-23'


@pytest.mark.asyncio
async def test_v23_content_state_migrates_original_work_day(tmp_path):
    import sqlite3

    path = tmp_path / 'old_v23_content.sqlite3'
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE members (
            user_id INTEGER PRIMARY KEY,
            display_name TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            joined_at TEXT NOT NULL
        );
        CREATE TABLE states (
            user_id INTEGER PRIMARY KEY,
            state TEXT NOT NULL,
            farming_started_at TEXT,
            content_started_at TEXT,
            content_return_state TEXT,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE farm_segments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            end_reason TEXT
        );
        CREATE TABLE content_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            proof_url TEXT,
            proof_message_id INTEGER,
            status TEXT NOT NULL DEFAULT 'active',
            reviewed_by INTEGER,
            reviewed_at TEXT
        );
    """)
    farm_start = '2026-08-23T23:00:00+03:00'
    content_start = '2026-08-23T23:30:00+03:00'
    con.execute(
        "INSERT INTO members(user_id,display_name,active,joined_at) VALUES(?,?,?,?)",
        (9200, 'LegacyContent', 1, farm_start)
    )
    con.execute(
        "INSERT INTO states(user_id,state,farming_started_at,content_started_at,content_return_state,updated_at) "
        "VALUES(?,?,?,?,?,?)",
        (9200, 'content', None, content_start, 'farming', content_start)
    )
    con.execute(
        "INSERT INTO farm_segments(user_id,started_at,ended_at,end_reason) VALUES(?,?,?,?)",
        (9200, farm_start, content_start, 'content')
    )
    con.execute(
        "INSERT INTO content_sessions(user_id,started_at,status) VALUES(?,?,?)",
        (9200, content_start, 'active')
    )
    con.commit()
    con.close()

    db = Database(path)
    await db.init()

    state = await db.get_state(9200)
    assert state['work_day'] == '2026-08-23'

    tz = ZoneInfo('Europe/Moscow')
    await db.return_from_content(9200, datetime(2026, 8, 24, 0, 30, tzinfo=tz))
    resumed = await db.get_state(9200)
    assert resumed['state'] == 'farming'
    assert resumed['work_day'] == '2026-08-23'
