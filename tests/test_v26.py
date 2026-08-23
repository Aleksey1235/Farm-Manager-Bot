from pathlib import Path


def test_manual_finish_branch_notifies_leadership():
    source = (Path(__file__).parents[1] / "farmbot" / "views.py").read_text(encoding="utf-8")
    manual_pos = source.find('elif result["status"] == "manual":')
    assert manual_pos != -1
    block = source[manual_pos:manual_pos + 900]
    assert "notify_pending_amount" in block
    assert "пересмотра суммы" in block


def test_discord_panel_version_is_current():
    source = (Path(__file__).parents[1] / "farmbot" / "bot.py").read_text(encoding="utf-8")
    assert 'FARM Manager Bot v2.7' in source
