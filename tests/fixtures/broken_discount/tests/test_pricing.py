from pricing import discounted_price


def test_below_threshold():
    assert discounted_price(80) == 80.0


def test_at_threshold():
    assert discounted_price(100) == 90.0


def test_above_threshold():
    assert discounted_price(120) == 108.0
