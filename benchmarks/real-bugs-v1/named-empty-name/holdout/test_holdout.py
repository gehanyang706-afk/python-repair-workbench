from boltons.namedutils import namedtuple, namedlist
import pytest

@pytest.mark.parametrize('factory', [namedtuple, namedlist])
def test_empty_field(factory):
    with pytest.raises(ValueError):
        factory('Point', [''])

@pytest.mark.parametrize('factory', [namedtuple, namedlist])
def test_empty_type(factory):
    with pytest.raises(ValueError):
        factory('', ['x'])

@pytest.mark.parametrize('factory', [namedtuple, namedlist])
def test_rename(factory):
    assert factory('Point', ['', 'x'], rename=True)(2,3).x == 3
