from boltons.iterutils import backoff_iter

def test_unit_factor_same_bounds():
    assert list(backoff_iter(2, 2, factor=1)) == [2]

def test_explicit_count():
    assert list(backoff_iter(2, 8, factor=1, count=3)) == [2,2,2]
