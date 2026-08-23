from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
import pytest
from farmbot.db import Database

ROOT = Path(__file__).parents[1]

def test_finish_acks_before_network_report():
    source = (ROOT / "farmbot/views.py").read_text(encoding="utf-8")
    pos = source.find("async def finish(self, interaction: discord.Interaction")
    block = source[pos:pos+5000]
    assert "response.defer(ephemeral=True)" in block
    assert "notify_pending_amount" in block
    assert block.index("response.defer") < block.index("notify_pending_amount")
    assert "followup.send" in block

def test_report_delivery_is_fresh_send_not_old_edit():
    source = (ROOT / "farmbot/bot.py").read_text(encoding="utf-8")
    pos = source.find("async def notify_pending_amount")
    block = source[pos:pos+5000]
    assert "message = await channel.send" in block
    assert "fetch_message(existing" not in block
    assert "save_daily_report_notification" in block

def test_report_channel_cache_fallback():
    source = (ROOT / "farmbot/bot.py").read_text(encoding="utf-8")
    pos = source.find("async def resolve_text_channel")
    block = source[pos:pos+1800]
    assert "self.get_channel" in block
    assert "await self.fetch_channel" in block

def test_admin_report_test_button_exists():
    source = (ROOT / "farmbot/views.py").read_text(encoding="utf-8")
    assert 'custom_id="admin:test_reports"' in source
    assert "Проверка FARM-отчётов" in source

@pytest.mark.asyncio
async def test_manual_latest_review_restores_after_restart(tmp_path):
    tz = ZoneInfo("UTC")
    db = Database(tmp_path / "review.sqlite3")
    await db.init()
    uid = 9901
    t = datetime(2026,8,24,10,0,tzinfo=tz)
    await db.ensure_member(uid, "Review", t)
    await db.start_farm(uid, t)
    await db.finish_farm(uid, datetime(2026,8,24,11,0,tzinfo=tz), tz)
    await db.assign_manual_amount(uid, "2026-08-24", 300000, 111, datetime(2026,8,24,11,1,tzinfo=tz))
    await db.save_daily_report_notification(uid, "2026-08-24", 123, 456)
    rows = await db.latest_daily_report_reviews()
    row = next(r for r in rows if r["user_id"] == uid)
    assert row["status"] == "manual"
    assert row["manual_amount"] == 300000
    assert row["channel_id"] == 123
    assert row["message_id"] == 456
