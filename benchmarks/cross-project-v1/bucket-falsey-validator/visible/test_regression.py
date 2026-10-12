from more_itertools import bucket
class RejectAll:
    def __bool__(self): return False
    def __call__(self,key): return False

def test_falsey_validator_filters_reads():
    b=bucket([1,2,3],key=lambda x:x,validator=RejectAll())
    assert list(b[1]) == []

def test_default_validator_keeps_groups():
    b=bucket([1,2,3],key=lambda x:x%2)
    assert list(b[1]) == [1,3]
    assert list(b[0]) == [2]
