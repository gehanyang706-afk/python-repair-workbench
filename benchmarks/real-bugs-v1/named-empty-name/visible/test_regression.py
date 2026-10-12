from boltons.namedutils import namedtuple
import pytest

def test_empty_type_name():
    with pytest.raises(ValueError):
        namedtuple('', ['x'])

def test_valid():
    assert namedtuple('Point', ['x'])(3).x == 3
