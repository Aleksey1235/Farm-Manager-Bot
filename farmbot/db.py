from __future__ import annotations

import asyncio
from datetime import datetime, date, timedelta
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Any

from .sqlite_async import AsyncConnection

from .calc import week_bounds


SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS members (
    user_id INTEGER PRIMARY KEY,
    display_name TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    joined_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS states (
    user_id INTEGER PRIMARY KEY REFERENCES members(user_id) ON DELETE CASCADE,
    state TEXT NOT NULL CHECK(state IN ('idle','farming','content')),
    farming_started_at TEXT,
    work_day TEXT,
    content_started_at TEXT,
    content_return_state TEXT CHECK(content_return_state IN ('idle','farming') OR content_return_state IS NULL),
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS farm_segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES members(user_id) ON DELETE CASCADE,
    work_day TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    end_reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_farm_segments_user_start ON farm_segments(user_id, started_at);

CREATE TABLE IF NOT EXISTS daily_reports (
    user_id INTEGER NOT NULL REFERENCES members(user_id) ON DELETE CASCADE,
    day TEXT NOT NULL,
    manual_amount INTEGER CHECK(manual_amount IS NULL OR manual_amount >= 0),
    manual_by INTEGER,
    manual_at TEXT,
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','pending','auto','manual')),
    last_end_at TEXT,
    PRIMARY KEY(user_id, day)
);

CREATE TABLE IF NOT EXISTS content_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES members(user_id) ON DELETE CASCADE,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    proof_url TEXT,
    proof_message_id INTEGER,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK(status IN ('active','awaiting_proof','pending_review','approved','rejected','cancelled')),
    reviewed_by INTEGER,
    reviewed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_content_user_start ON content_sessions(user_id, started_at);

CREATE TABLE IF NOT EXISTS weekly_payments (
    user_id INTEGER NOT NULL REFERENCES members(user_id) ON DELETE CASCADE,
    week_start TEXT NOT NULL,
    amount_due INTEGER,
    amount_paid INTEGER,
    proof_url TEXT,
    status TEXT NOT NULL DEFAULT 'open'
        CHECK(status IN ('open','pending','paid','rejected')),
    reviewed_by INTEGER,
    reviewed_at TEXT,
    PRIMARY KEY(user_id, week_start)
);

CREATE TABLE IF NOT EXISTS panels (
    name TEXT PRIMARY KEY,
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_report_notifications (
    user_id INTEGER NOT NULL REFERENCES members(user_id) ON DELETE CASCADE,
    day TEXT NOT NULL,
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    PRIMARY KEY(user_id, day)
);

CREATE TABLE IF NOT EXISTS weekly_report_runs (
    week_start TEXT PRIMARY KEY,
    sent_at TEXT NOT NULL,
    message_id INTEGER
);

CREATE TABLE IF NOT EXISTS session_reminders (
    user_id INTEGER NOT NULL REFERENCES members(user_id) ON DELETE CASCADE,
    segment_started_at TEXT NOT NULL,
    reminded_at TEXT NOT NULL,
    resolved_at TEXT,
    resolution TEXT CHECK(resolution IN ('continue','finish') OR resolution IS NULL),
    PRIMARY KEY(user_id, segment_started_at)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    actor_id INTEGER,
    action TEXT NOT NULL,
    target_user_id INTEGER,
    details TEXT
);
"""


DEFAULT_SETTINGS = {
    "farm_threshold_minutes": "240",
    "auto_daily_amount": "100000",
    "content_discount": "50000",
    "minimum_weekly_due": "0",
    "active_refresh_seconds": "15",
    "max_single_segment_hours": "12",
    "session_reminder_minutes": "360",
    "weekly_report_enabled": "1",
    "weekly_report_weekday": "6",
    "weekly_report_hour": "23",
    "weekly_report_minute": "0",
}


class Database:
    def __init__(self, path: Path):
        self.path = path
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = AsyncConnection(self.path)
        await db.execute("PRAGMA foreign_keys = ON")
        try:
            yield db
        finally:
            await db.close()

    async def init(self):
        async with self.connect() as db:
            await db.executescript(SCHEMA)

            state_cols = {r["name"] for r in await (await db.execute("PRAGMA table_info(states)")).fetchall()}
            if "work_day" not in state_cols:
                await db.execute("ALTER TABLE states ADD COLUMN work_day TEXT")

            seg_cols = {r["name"] for r in await (await db.execute("PRAGMA table_info(farm_segments)")).fetchall()}
            if "work_day" not in seg_cols:
                await db.execute("ALTER TABLE farm_segments ADD COLUMN work_day TEXT")

            rows = await (await db.execute("SELECT id,started_at FROM farm_segments WHERE work_day IS NULL")).fetchall()
            for row in rows:
                try:
                    work_day = datetime.fromisoformat(row["started_at"]).date().isoformat()
                except Exception:
                    work_day = str(row["started_at"])[:10]
                await db.execute("UPDATE farm_segments SET work_day=? WHERE id=?", (work_day, row["id"]))

            active_rows = await (await db.execute(
                "SELECT user_id,farming_started_at FROM states "
                "WHERE work_day IS NULL AND farming_started_at IS NOT NULL"
            )).fetchall()
            for row in active_rows:
                try:
                    work_day = datetime.fromisoformat(row["farming_started_at"]).date().isoformat()
                except Exception:
                    work_day = str(row["farming_started_at"])[:10]
                await db.execute(
                    "UPDATE states SET work_day=? WHERE user_id=?",
                    (work_day, row["user_id"])
                )

            # Если обновление произошло прямо во время контента, farming_started_at
            # уже NULL. Восстанавливаем рабочий день из последнего фарм-сегмента.
            content_rows = await (await db.execute(
                "SELECT user_id FROM states "
                "WHERE work_day IS NULL AND state='content' AND content_return_state='farming'"
            )).fetchall()
            for row in content_rows:
                seg = await (await db.execute(
                    "SELECT work_day,started_at FROM farm_segments "
                    "WHERE user_id=? ORDER BY id DESC LIMIT 1",
                    (row["user_id"],)
                )).fetchone()
                if not seg:
                    continue
                work_day = seg["work_day"]
                if not work_day:
                    try:
                        work_day = datetime.fromisoformat(seg["started_at"]).date().isoformat()
                    except Exception:
                        work_day = str(seg["started_at"])[:10]
                await db.execute(
                    "UPDATE states SET work_day=? WHERE user_id=?",
                    (work_day, row["user_id"])
                )

            for k, v in DEFAULT_SETTINGS.items():
                await db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))
            await db.commit()

    async def setting_int(self, key: str) -> int:
        async with self.connect() as db:
            row = await (await db.execute("SELECT value FROM settings WHERE key=?", (key,))).fetchone()
            if not row:
                raise KeyError(key)
            return int(row["value"])

    async def set_setting(self, key: str, value: int):
        async with self.connect() as db:
            await db.execute("""
                INSERT INTO settings(key,value) VALUES(?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """, (key, str(int(value))))
            await db.commit()

    async def setting_str(self, key: str) -> str:
        async with self.connect() as db:
            row = await (await db.execute("SELECT value FROM settings WHERE key=?", (key,))).fetchone()
            if not row:
                raise KeyError(key)
            return str(row["value"])

    async def set_setting_str(self, key: str, value: str):
        async with self.connect() as db:
            await db.execute("""
                INSERT INTO settings(key,value) VALUES(?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """, (key, str(value)))
            await db.commit()

    async def ensure_member(self, user_id: int, display_name: str, now: datetime):
        async with self.connect() as db:
            await db.execute("""
                INSERT INTO members(user_id,display_name,joined_at) VALUES(?,?,?)
                ON CONFLICT(user_id) DO UPDATE SET display_name=excluded.display_name, active=1
            """, (user_id, display_name, now.isoformat()))
            await db.execute("""
                INSERT OR IGNORE INTO states(user_id,state,updated_at) VALUES(?, 'idle', ?)
            """, (user_id, now.isoformat()))
            await db.commit()

    async def get_state(self, user_id: int) -> dict[str, Any]:
        async with self.connect() as db:
            row = await (await db.execute("SELECT * FROM states WHERE user_id=?", (user_id,))).fetchone()
            return dict(row) if row else {}

    async def start_farm(self, user_id: int, now: datetime):
        async with self._lock:
            async with self.connect() as db:
                row = await (await db.execute("SELECT * FROM states WHERE user_id=?", (user_id,))).fetchone()
                if not row or row["state"] != "idle":
                    raise ValueError("Фарм можно начать только из состояния «не фармит».")
                work_day = now.date().isoformat()
                await db.execute("""
                    UPDATE states SET state='farming', farming_started_at=?, work_day=?, content_started_at=NULL,
                    content_return_state=NULL, updated_at=? WHERE user_id=?
                """, (now.isoformat(), work_day, now.isoformat(), user_id))
                await db.execute("""
                    INSERT INTO farm_segments(user_id,work_day,started_at) VALUES(?,?,?)
                """, (user_id, work_day, now.isoformat()))
                await db.commit()

    async def _close_open_farm_segment(self, db, user_id: int, now: datetime, reason: str):
        row = await (await db.execute("""
            SELECT id FROM farm_segments WHERE user_id=? AND ended_at IS NULL ORDER BY id DESC LIMIT 1
        """, (user_id,))).fetchone()
        if row:
            await db.execute("UPDATE farm_segments SET ended_at=?, end_reason=? WHERE id=?",
                             (now.isoformat(), reason, row["id"]))

    async def pause_for_content(self, user_id: int, now: datetime):
        async with self._lock:
            async with self.connect() as db:
                row = await (await db.execute("SELECT * FROM states WHERE user_id=?", (user_id,))).fetchone()
                if not row or row["state"] == "content":
                    raise ValueError("Вы уже на контенте.")
                return_state = row["state"]
                if row["state"] == "farming":
                    await self._close_open_farm_segment(db, user_id, now, "content")
                await db.execute("""
                    UPDATE states SET state='content', farming_started_at=NULL, content_started_at=?,
                    content_return_state=?, updated_at=? WHERE user_id=?
                """, (now.isoformat(), return_state, now.isoformat(), user_id))
                await db.execute("""
                    INSERT INTO content_sessions(user_id,started_at,status) VALUES(?,?,'active')
                """, (user_id, now.isoformat()))
                await db.commit()

    async def return_from_content(self, user_id: int, now: datetime) -> int:
        async with self._lock:
            async with self.connect() as db:
                state = await (await db.execute("SELECT * FROM states WHERE user_id=?", (user_id,))).fetchone()
                if not state or state["state"] != "content":
                    raise ValueError("Сейчас вы не на контенте.")
                content = await (await db.execute("""
                    SELECT id FROM content_sessions
                    WHERE user_id=? AND status='active' ORDER BY id DESC LIMIT 1
                """, (user_id,))).fetchone()
                if not content:
                    raise ValueError("Активный контент не найден.")
                await db.execute("""
                    UPDATE content_sessions SET ended_at=?, status='awaiting_proof' WHERE id=?
                """, (now.isoformat(), content["id"]))
                next_state = state["content_return_state"] or "idle"
                work_day = state["work_day"]
                if next_state == "farming":
                    if not work_day:
                        work_day = now.date().isoformat()
                    await db.execute("""
                        INSERT INTO farm_segments(user_id,work_day,started_at) VALUES(?,?,?)
                    """, (user_id, work_day, now.isoformat()))
                    farm_started = now.isoformat()
                else:
                    farm_started = None
                await db.execute("""
                    UPDATE states SET state=?, farming_started_at=?, work_day=?, content_started_at=NULL,
                    content_return_state=NULL, updated_at=? WHERE user_id=?
                """, (next_state, farm_started, work_day, now.isoformat(), user_id))
                await db.commit()
                return int(content["id"])

    async def finish_farm(self, user_id: int, now: datetime, tz) -> dict[str, Any]:
        async with self._lock:
            async with self.connect() as db:
                row = await (await db.execute("SELECT * FROM states WHERE user_id=?", (user_id,))).fetchone()
                if not row or row["state"] != "farming":
                    raise ValueError("Сейчас у вас нет активного фарма.")

                work_day = row["work_day"]
                if not work_day:
                    work_day = datetime.fromisoformat(row["farming_started_at"]).date().isoformat()

                await self._close_open_farm_segment(db, user_id, now, "finish")
                await db.execute("""
                    UPDATE states SET state='idle', farming_started_at=NULL, work_day=NULL, updated_at=? WHERE user_id=?
                """, (now.isoformat(), user_id))
                day = work_day
                await db.execute("""
                    INSERT INTO daily_reports(user_id,day,status,last_end_at) VALUES(?,?,'pending',?)
                    ON CONFLICT(user_id,day) DO UPDATE SET last_end_at=excluded.last_end_at
                """, (user_id, day, now.isoformat()))
                await db.commit()

        work_date = date.fromisoformat(day)
        total = await self.farm_seconds_for_day(user_id, work_date, tz, now)
        threshold = await self.setting_int("farm_threshold_minutes")
        auto_amount = await self.setting_int("auto_daily_amount")
        if total >= threshold * 60:
            async with self.connect() as db:
                await db.execute("""
                    UPDATE daily_reports SET manual_amount=NULL, manual_by=NULL, manual_at=NULL, status='auto'
                    WHERE user_id=? AND day=?
                """, (user_id, day))
                await db.commit()
            return {"seconds": total, "status": "auto", "amount": auto_amount, "work_day": day}

        async with self.connect() as db:
            rr = await (await db.execute(
                "SELECT manual_amount FROM daily_reports WHERE user_id=? AND day=?", (user_id, day)
            )).fetchone()
        return {
            "seconds": total,
            "status": "manual" if rr and rr["manual_amount"] is not None else "pending",
            "amount": rr["manual_amount"] if rr else None,
            "work_day": day,
        }

    async def farm_seconds_for_day(self, user_id: int, day: date, tz, now: datetime) -> int:
        day_key = day.isoformat()
        async with self.connect() as db:
            rows = await (await db.execute("""
                SELECT started_at, ended_at FROM farm_segments
                WHERE user_id=? AND work_day=?
            """, (user_id, day_key))).fetchall()

        total = 0
        for r in rows:
            started = datetime.fromisoformat(r["started_at"])
            ended = datetime.fromisoformat(r["ended_at"]) if r["ended_at"] else now
            total += max(0, int((ended - started).total_seconds()))
        return total

    async def active_states(self):
        async with self.connect() as db:
            rows = await (await db.execute("""
                SELECT m.user_id,m.display_name,s.state,s.farming_started_at,s.work_day,s.content_started_at
                FROM states s JOIN members m USING(user_id)
                WHERE s.state IN ('farming','content') ORDER BY s.updated_at
            """)).fetchall()
            return [dict(r) for r in rows]

    async def assign_manual_amount(self, user_id: int, day: str, amount: int, actor_id: int, now: datetime):
        if amount < 0:
            raise ValueError("Сумма не может быть отрицательной.")
        threshold = await self.setting_int("farm_threshold_minutes")
        total = await self.farm_seconds_for_day(user_id, date.fromisoformat(day), now.tzinfo, now)
        if total >= threshold * 60:
            raise ValueError("У игрока уже 4+ часа: сумма назначается автоматически.")
        async with self.connect() as db:
            await db.execute("""
                INSERT INTO daily_reports(user_id,day,manual_amount,manual_by,manual_at,status)
                VALUES(?,?,?,?,?,'manual')
                ON CONFLICT(user_id,day) DO UPDATE SET manual_amount=excluded.manual_amount,
                manual_by=excluded.manual_by, manual_at=excluded.manual_at, status='manual'
            """, (user_id, day, amount, actor_id, now.isoformat()))
            await db.commit()

    async def pending_daily_reports(self):
        async with self.connect() as db:
            rows = await (await db.execute("""
                SELECT d.user_id,d.day,d.manual_amount,d.status,m.display_name
                FROM daily_reports d JOIN members m USING(user_id)
                WHERE d.status='pending'
                ORDER BY d.day DESC, m.display_name
            """)).fetchall()
            return [dict(r) for r in rows]

    async def set_content_proof(self, user_id: int, proof_url: str, message_id: int) -> int:
        async with self.connect() as db:
            row = await (await db.execute("""
                SELECT id FROM content_sessions WHERE user_id=? AND status='awaiting_proof'
                ORDER BY id DESC LIMIT 1
            """, (user_id,))).fetchone()
            if not row:
                raise ValueError("Нет контента, ожидающего фото.")
            await db.execute("""
                UPDATE content_sessions SET proof_url=?,proof_message_id=?,status='pending_review' WHERE id=?
            """, (proof_url, message_id, row["id"]))
            await db.commit()
            return int(row["id"])

    async def review_content(self, content_id: int, approve: bool, actor_id: int, now: datetime):
        async with self.connect() as db:
            row = await (await db.execute("SELECT status FROM content_sessions WHERE id=?", (content_id,))).fetchone()
            if not row or row["status"] != "pending_review":
                raise ValueError("Этот отчёт уже обработан или не существует.")
            await db.execute("""
                UPDATE content_sessions SET status=?,reviewed_by=?,reviewed_at=? WHERE id=?
            """, ("approved" if approve else "rejected", actor_id, now.isoformat(), content_id))
            await db.commit()

    async def member_week_summary_for_range(
        self, user_id: int, week_start: date, week_end: date, tz, now: datetime,
        current_day: date | None = None
    ) -> dict[str, Any]:
        threshold = await self.setting_int("farm_threshold_minutes")
        auto_amount = await self.setting_int("auto_daily_amount")
        discount = await self.setting_int("content_discount")
        minimum = await self.setting_int("minimum_weekly_due")

        if current_day is None:
            current_day = now.astimezone(tz).date()

        days = []
        payable_amounts = []
        total_seconds = 0

        start_dt = datetime.combine(week_start, datetime.min.time(), tzinfo=tz)
        end_dt = datetime.combine(week_end + timedelta(days=1), datetime.min.time(), tzinfo=tz)

        async with self.connect() as db:
            reports = await (await db.execute("""
                SELECT * FROM daily_reports WHERE user_id=? AND day BETWEEN ? AND ?
            """, (user_id, week_start.isoformat(), week_end.isoformat()))).fetchall()
            rmap = {r["day"]: dict(r) for r in reports}

            c = await (await db.execute("""
                SELECT COUNT(*) AS c FROM content_sessions
                WHERE user_id=? AND status='approved'
                  AND started_at >= ? AND started_at < ?
            """, (user_id, start_dt.isoformat(), end_dt.isoformat()))).fetchone()
            approved_contents = int(c["c"])

            state = await (await db.execute(
                "SELECT state FROM states WHERE user_id=?", (user_id,)
            )).fetchone()
            current_state = state["state"] if state else "idle"

        unresolved_finished_days = 0
        d = week_start
        last_day_to_show = min(week_end, current_day) if week_start <= current_day else week_end

        while d <= last_day_to_show:
            sec = await self.farm_seconds_for_day(user_id, d, tz, now)
            total_seconds += sec
            rr = rmap.get(d.isoformat())

            if sec == 0 and rr is None:
                days.append({
                    "day": d, "seconds": 0, "amount": None,
                    "status": "inactive", "counts_for_due": False
                })
                d += timedelta(days=1)
                continue

            if sec >= threshold * 60:
                amount = auto_amount
                status = "auto"
                payable_amounts.append(amount)
            elif rr and rr.get("manual_amount") is not None:
                amount = int(rr["manual_amount"])
                status = "manual"
                payable_amounts.append(amount)
            elif rr and rr.get("status") == "pending":
                amount = None
                status = "pending"
                unresolved_finished_days += 1
            else:
                amount = None
                status = "farming" if d == current_day and current_state in ("farming", "content") else "open"

            days.append({
                "day": d, "seconds": sec, "amount": amount,
                "status": status,
                "counts_for_due": status in ("auto", "manual", "pending")
            })
            d += timedelta(days=1)

        gross = sum(payable_amounts)
        due = None if unresolved_finished_days else max(
            minimum, gross - approved_contents * discount
        )

        return {
            "week_start": week_start, "week_end": week_end, "days": days,
            "total_seconds": total_seconds, "approved_contents": approved_contents,
            "discount_total": approved_contents * discount,
            "gross": gross,
            "due": due,
            "content_discount": discount,
            "unresolved_finished_days": unresolved_finished_days,
            "active_days": sum(1 for x in days if x["seconds"] > 0),
        }

    async def member_week_summary(self, user_id: int, today: date, tz, now: datetime) -> dict[str, Any]:
        wb = week_bounds(today)
        return await self.member_week_summary_for_range(
            user_id, wb.start, wb.end, tz, now, current_day=today
        )

    async def department_week_stats_for_range(
        self, week_start: date, week_end: date, tz, now: datetime
    ):
        async with self.connect() as db:
            members = await (await db.execute(
                "SELECT user_id,display_name,active FROM members"
            )).fetchall()
        rows = []
        for m in members:
            s = await self.member_week_summary_for_range(
                int(m["user_id"]), week_start, week_end, tz, now
            )
            include = bool(m["active"]) or any((
                s["total_seconds"],
                s["approved_contents"],
                s["gross"],
                s["unresolved_finished_days"],
            ))
            if not include:
                continue
            rows.append({
                "user_id": int(m["user_id"]),
                "display_name": m["display_name"],
                **s
            })
        rows.sort(key=lambda x: x["total_seconds"], reverse=True)
        return rows

    async def department_week_stats(self, today: date, tz, now: datetime):
        wb = week_bounds(today)
        return await self.department_week_stats_for_range(wb.start, wb.end, tz, now)

    async def department_snapshot(self, week_start: date, week_end: date, tz, now: datetime):
        rows = await self.department_week_stats_for_range(week_start, week_end, tz, now)
        payments = await self.payment_overview_shallow(week_start, rows)
        active = [r for r in rows if r["total_seconds"] > 0]
        total_seconds = sum(r["total_seconds"] for r in rows)
        total_contents = sum(r["approved_contents"] for r in rows)
        gross = sum(r["gross"] for r in rows)
        discounts = sum(r["discount_total"] for r in rows)
        ready_due = sum(r["due"] for r in rows if r["due"] is not None)
        unresolved = sum(1 for r in rows if r["due"] is None)
        avg_seconds = int(total_seconds / len(active)) if active else 0
        return {
            "week_start": week_start,
            "week_end": week_end,
            "members": len(rows),
            "active_members": len(active),
            "total_seconds": total_seconds,
            "avg_seconds": avg_seconds,
            "contents": total_contents,
            "gross": gross,
            "discounts": discounts,
            "ready_due": ready_due,
            "unresolved": unresolved,
            "paid": len(payments["paid"]),
            "payment_pending": len(payments["pending"]),
            "unpaid": len(payments["unpaid"]),
            "not_required": len(payments["not_required"]),
            "rows": rows,
        }

    async def payment_overview_shallow(self, week_start: date, rows: list[dict[str, Any]]):
        async with self.connect() as db:
            payments = await (await db.execute("""
                SELECT user_id,status,amount_due,amount_paid
                FROM weekly_payments WHERE week_start=?
            """, (week_start.isoformat(),))).fetchall()
            pmap = {int(r["user_id"]): dict(r) for r in payments}

        result = {"paid": [], "pending": [], "unpaid": [], "unresolved": [], "not_required": []}
        for r in rows:
            if r["due"] is None:
                result["unresolved"].append(r)
                continue
            if r["due"] <= 0:
                result["not_required"].append(r)
                continue
            status = pmap.get(r["user_id"], {}).get("status", "open")
            item = {**r, "payment_status": status}
            if status == "paid":
                result["paid"].append(item)
            elif status == "pending":
                result["pending"].append(item)
            else:
                result["unpaid"].append(item)
        return result

    async def daily_report_notification(self, user_id: int, day: str):
        async with self.connect() as db:
            row = await (await db.execute("""
                SELECT channel_id,message_id FROM daily_report_notifications
                WHERE user_id=? AND day=?
            """, (user_id, day))).fetchone()
            return dict(row) if row else None

    async def save_daily_report_notification(
        self, user_id: int, day: str, channel_id: int, message_id: int
    ):
        async with self.connect() as db:
            await db.execute("""
                INSERT INTO daily_report_notifications(user_id,day,channel_id,message_id)
                VALUES(?,?,?,?)
                ON CONFLICT(user_id,day) DO UPDATE SET
                    channel_id=excluded.channel_id,
                    message_id=excluded.message_id
            """, (user_id, day, channel_id, message_id))
            await db.commit()

    async def sync_active_members(self, active_ids: set[int]):
        async with self.connect() as db:
            await db.execute("UPDATE members SET active=0")
            for uid in active_ids:
                await db.execute("UPDATE members SET active=1 WHERE user_id=?", (uid,))
            await db.commit()

    async def pending_content_reviews(self):
        async with self.connect() as db:
            rows = await (await db.execute("""
                SELECT id,user_id FROM content_sessions WHERE status='pending_review'
            """)).fetchall()
            return [dict(r) for r in rows]

    async def set_payment_proof(self, user_id: int, week_start: str, amount_due: int,
                                proof_url: str) -> None:
        async with self.connect() as db:
            row = await (await db.execute("""
                SELECT status FROM weekly_payments WHERE user_id=? AND week_start=?
            """, (user_id, week_start))).fetchone()
            if row and row["status"] == "paid":
                raise ValueError("Недельный взнос уже подтверждён.")
            await db.execute("""
                INSERT INTO weekly_payments(user_id,week_start,amount_due,amount_paid,proof_url,status)
                VALUES(?,?,?,?,?,'pending')
                ON CONFLICT(user_id,week_start) DO UPDATE SET
                    amount_due=excluded.amount_due,
                    amount_paid=excluded.amount_paid,
                    proof_url=excluded.proof_url,
                    status='pending',
                    reviewed_by=NULL,
                    reviewed_at=NULL
            """, (user_id, week_start, amount_due, amount_due, proof_url))
            await db.commit()

    async def review_payment(self, user_id: int, week_start: str, approve: bool,
                             actor_id: int, now: datetime):
        async with self.connect() as db:
            row = await (await db.execute("""
                SELECT status FROM weekly_payments WHERE user_id=? AND week_start=?
            """, (user_id, week_start))).fetchone()
            if not row or row["status"] != "pending":
                raise ValueError("Этот взнос уже обработан или не найден.")
            await db.execute("""
                UPDATE weekly_payments
                SET status=?,reviewed_by=?,reviewed_at=?
                WHERE user_id=? AND week_start=?
            """, ("paid" if approve else "rejected", actor_id, now.isoformat(), user_id, week_start))
            await db.commit()

    async def payment_status(self, user_id: int, week_start: str) -> str:
        async with self.connect() as db:
            row = await (await db.execute("""
                SELECT status FROM weekly_payments WHERE user_id=? AND week_start=?
            """, (user_id, week_start))).fetchone()
            return row["status"] if row else "open"

    async def pending_payment_reviews(self):
        async with self.connect() as db:
            rows = await (await db.execute("""
                SELECT user_id,week_start FROM weekly_payments WHERE status='pending'
            """)).fetchall()
            return [dict(r) for r in rows]

    async def report_was_sent(self, week_start: date) -> bool:
        async with self.connect() as db:
            row = await (await db.execute(
                "SELECT 1 FROM weekly_report_runs WHERE week_start=?",
                (week_start.isoformat(),)
            )).fetchone()
            return bool(row)

    async def mark_report_sent(self, week_start: date, sent_at: datetime, message_id: int | None):
        async with self.connect() as db:
            await db.execute("""
                INSERT INTO weekly_report_runs(week_start,sent_at,message_id)
                VALUES(?,?,?)
                ON CONFLICT(week_start) DO UPDATE SET
                    sent_at=excluded.sent_at,
                    message_id=excluded.message_id
            """, (week_start.isoformat(), sent_at.isoformat(), message_id))
            await db.commit()

    async def create_session_reminder(self, user_id: int, segment_started_at: str, now: datetime) -> bool:
        async with self.connect() as db:
            try:
                await db.execute("""
                    INSERT INTO session_reminders(user_id,segment_started_at,reminded_at)
                    VALUES(?,?,?)
                """, (user_id, segment_started_at, now.isoformat()))
                await db.commit()
                return True
            except Exception:
                return False

    async def resolve_session_reminder(self, user_id: int, segment_started_at: str,
                                       resolution: str, now: datetime):
        if resolution not in ("continue", "finish"):
            raise ValueError("Некорректное решение.")
        async with self.connect() as db:
            await db.execute("""
                UPDATE session_reminders
                SET resolved_at=?, resolution=?
                WHERE user_id=? AND segment_started_at=? AND resolved_at IS NULL
            """, (now.isoformat(), resolution, user_id, segment_started_at))
            await db.commit()

    async def unresolved_session_reminders(self):
        async with self.connect() as db:
            rows = await (await db.execute("""
                SELECT r.user_id,r.segment_started_at,r.reminded_at
                FROM session_reminders r
                JOIN states s ON s.user_id=r.user_id
                WHERE r.resolved_at IS NULL
                  AND s.state='farming'
                  AND s.farming_started_at=r.segment_started_at
            """)).fetchall()
            return [dict(r) for r in rows]

    async def session_reminder_exists(self, user_id: int, segment_started_at: str) -> bool:
        async with self.connect() as db:
            row = await (await db.execute("""
                SELECT 1 FROM session_reminders
                WHERE user_id=? AND segment_started_at=?
            """, (user_id, segment_started_at))).fetchone()
            return bool(row)

    async def member_known(self, user_id: int) -> bool:
        async with self.connect() as db:
            row = await (await db.execute(
                "SELECT 1 FROM members WHERE user_id=? AND active=1", (user_id,)
            )).fetchone()
            return bool(row)

    async def payment_overview(self, week_start: date, week_end: date, tz, now: datetime):
        rows = await self.department_week_stats_for_range(week_start, week_end, tz, now)
        async with self.connect() as db:
            payments = await (await db.execute("""
                SELECT user_id,status,amount_due,amount_paid
                FROM weekly_payments WHERE week_start=?
            """, (week_start.isoformat(),))).fetchall()
            pmap = {int(r["user_id"]): dict(r) for r in payments}

        result = {"paid": [], "pending": [], "unpaid": [], "unresolved": [], "not_required": []}
        for r in rows:
            if r["due"] is None:
                result["unresolved"].append(r)
                continue
            if r["due"] <= 0:
                result["not_required"].append(r)
                continue
            p = pmap.get(r["user_id"])
            status = p["status"] if p else "open"
            item = {**r, "payment_status": status}
            if status == "paid":
                result["paid"].append(item)
            elif status == "pending":
                result["pending"].append(item)
            else:
                result["unpaid"].append(item)
        return result

    async def get_panel(self, name: str):
        async with self.connect() as db:
            row = await (await db.execute("SELECT * FROM panels WHERE name=?", (name,))).fetchone()
            return dict(row) if row else None

    async def save_panel(self, name: str, channel_id: int, message_id: int):
        async with self.connect() as db:
            await db.execute("""
                INSERT INTO panels(name,channel_id,message_id) VALUES(?,?,?)
                ON CONFLICT(name) DO UPDATE SET channel_id=excluded.channel_id,message_id=excluded.message_id
            """, (name, channel_id, message_id))
            await db.commit()

    async def audit(self, now: datetime, action: str, actor_id: int | None = None,
                    target_user_id: int | None = None, details: str | None = None):
        async with self.connect() as db:
            await db.execute("""
                INSERT INTO audit_log(created_at,actor_id,action,target_user_id,details)
                VALUES(?,?,?,?,?)
            """, (now.isoformat(), actor_id, action, target_user_id, details))
            await db.commit()
