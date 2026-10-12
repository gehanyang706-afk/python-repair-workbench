import pytest
from boltons.iterutils import backoff_iter

@pytest.mark.parametrize('value', [1, 2, 3.5])
def test_same_bounds(value):
    assert list(backoff_iter(value, value, factor=1)) == [value]

@pytest.mark.parametrize('start,stop', [(0,1),(1,5),(2,10)])
def test_no_growth_rejected(start,stop):
    with pytest.raises(ValueError):
        list(backoff_iter(start,stop,factor=1))

def test_normal_growth():
    assert list(backoff_iter(1,4)) == [1,2,4]

def test_zero_stop_rejected():
    with pytest.raises(ValueError):
        list(backoff_iter(0,0,factor=1))
