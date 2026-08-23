from pathlib import Path

ROOT = Path(__file__).parents[1]

def test_assigned_amount_view_is_imported():
    source = (ROOT / "farmbot/bot.py").read_text(encoding="utf-8")
    imports = [line for line in source.splitlines() if line.startswith("from .views import")]
    assert imports
    assert "AssignedAmountView" in " ".join(imports)

def test_repeat_subthreshold_sends_fresh_report():
    source = (ROOT / "farmbot/bot.py").read_text(encoding="utf-8")
    pos = source.find("async def notify_pending_amount")
    block = source[pos:pos+5000]
    assert "message = await channel.send" in block
    assert "Повторное завершение фарма" in block
    assert "AssignedAmountView" in block

def test_panel_version_is_v28():
    source = (ROOT / "farmbot/bot.py").read_text(encoding="utf-8")
    assert "FARM Manager Bot v2.8" in source
