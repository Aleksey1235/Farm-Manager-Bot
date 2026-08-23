from pathlib import Path

ROOT = Path(__file__).parents[1]

def test_manual_finish_still_notifies_leadership():
    source = (ROOT / "farmbot/views.py").read_text(encoding="utf-8")
    pos = source.find('elif result["status"] == "manual":')
    assert pos != -1
    block = source[pos:pos+1500]
    assert "notify_pending_amount" in block
    assert "report_ok" in block

def test_panel_version_current():
    source = (ROOT / "farmbot/bot.py").read_text(encoding="utf-8")
    assert "FARM Manager Bot v2.8" in source
