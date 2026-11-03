from calculator import add


def test_add():
    assert add(2, 3) == 5


def test_zero():
    assert add(0, 0) == 0
