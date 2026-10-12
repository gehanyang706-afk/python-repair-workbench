from more_itertools import bucket
class FalseyEven:
    def __len__(self): return 0
    def __call__(self,key): return key%2 == 0

def test_falsey_validator_membership_and_iteration():
    b=bucket([1,2,3,4],key=lambda x:x,validator=FalseyEven())
    assert 1 not in b
    assert 2 in b
    assert set(b) == {2,4}
    assert list(b[2]) == [2]
    assert list(b[3]) == []

def test_none_and_truthy_validator():
    assert set(bucket([1,2],key=lambda x:x,validator=None)) == {1,2}
    b=bucket([1,2,3,4],key=lambda x:x%2,validator=lambda k:k==1)
    assert list(b[0]) == []
    assert list(b[1]) == [1,3]
