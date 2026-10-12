import pytest
from more_itertools import constrained_batches

def test_zero_count_rejected():
    with pytest.raises(ValueError):
        list(constrained_batches([b'a',b'b'],4,max_count=0))

def test_valid_count():
    assert list(constrained_batches([b'a',b'b',b'c'],4,max_count=2)) == [(b'a',b'b'),(b'c',)]
