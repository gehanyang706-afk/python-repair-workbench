import pytest
from more_itertools import constrained_batches

@pytest.mark.parametrize('count',[-1,-20,0])
@pytest.mark.parametrize('items',[[],[b'a',b'b']])
def test_invalid_count_independent_of_input(count,items):
    with pytest.raises(ValueError):
        list(constrained_batches(items,4,max_count=count))

def test_existing_limits_and_strict_semantics():
    assert list(constrained_batches([b'a',b'bb',b'c'],3,max_count=None)) == [(b'a',b'bb'),(b'c',)]
    assert list(constrained_batches([b'abcd'],2,strict=False)) == [(b'abcd',)]
    with pytest.raises(ValueError): list(constrained_batches([b'abcd'],2))
    with pytest.raises(ValueError): list(constrained_batches([],0))
    assert list(constrained_batches([],3,max_count=1)) == []
