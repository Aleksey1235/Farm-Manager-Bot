from farmbot.calc import daily_charge, weekly_due, format_duration


def test_under_threshold_without_manual_is_pending():
    assert daily_charge(3*3600, 240, 100000, None) == ("pending", None)


def test_under_threshold_with_manual():
    assert daily_charge(3*3600, 240, 100000, 275000) == ("manual", 275000)


def test_exact_threshold_is_auto():
    assert daily_charge(4*3600, 240, 100000, 999999) == ("auto", 100000)


def test_weekly_discount():
    due, discount = weekly_due([100000, 250000, 100000], 2, 50000, 0)
    assert discount == 100000
    assert due == 350000


def test_weekly_due_pending_if_any_day_unresolved():
    due, discount = weekly_due([100000, None], 2, 50000, 0)
    assert due is None
    assert discount == 100000


def test_minimum_weekly_due():
    due, _ = weekly_due([100000], 5, 50000, 50000)
    assert due == 50000


def test_format_duration():
    assert format_duration(3661) == "01:01:01"
