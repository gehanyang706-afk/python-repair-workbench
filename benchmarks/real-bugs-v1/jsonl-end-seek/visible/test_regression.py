import io
from boltons.jsonutils import JSONLIterator

def test_explicit_end():
    assert list(JSONLIterator(io.BytesIO(b'1\n2\n'), reverse=True, rel_seek=1.0)) == [2,1]

def test_forward():
    assert list(JSONLIterator(io.BytesIO(b'1\n2\n'))) == [1,2]
