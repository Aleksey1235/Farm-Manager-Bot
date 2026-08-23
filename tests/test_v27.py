from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_assigned_amount_view_is_imported_in_bot():
    source = (ROOT / "farmbot" / "bot.py").read_text(encoding="utf-8")
    import_lines = [line for line in source.splitlines() if line.startswith("from .views import")]
    assert import_lines, "views import not found"
    assert "AssignedAmountView" in " ".join(import_lines)


def test_assigned_amount_view_is_defined():
    source = (ROOT / "farmbot" / "views.py").read_text(encoding="utf-8")
    assert "class AssignedAmountView" in source


def test_repeat_subthreshold_flow_uses_assigned_amount_view():
    source = (ROOT / "farmbot" / "bot.py").read_text(encoding="utf-8")
    pos = source.find("existing = await self.db.daily_report_notification")
    assert pos != -1
    block = source[pos:pos + 2500]
    assert "AssignedAmountView" in block
    assert "Оставьте прежнюю сумму" in block or "Общее время изменилось" in block


def test_panel_version_is_v27():
    source = (ROOT / "farmbot" / "bot.py").read_text(encoding="utf-8")
    assert "FARM Manager Bot v2.7" in source
