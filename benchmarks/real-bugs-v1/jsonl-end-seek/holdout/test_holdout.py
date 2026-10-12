import io
import pytest
from boltons.jsonutils import JSONLIterator

@pytest.mark.parametrize('text,expected', [('', []), ('3\n', [3]), ('1\n2\n3\n', [3,2,1])])
def test_end_reverse(text,expected):
    assert list(JSONLIterator(io.BytesIO(text.encode()), reverse=True, rel_seek=1.0)) == expected

def test_end_forward():
    assert list(JSONLIterator(io.BytesIO(b'1\n2\n'), rel_seek=1.0)) == []

@pytest.mark.parametrize('value', [-1.0, -2, 1.01, 2])
def test_invalid(value):
    with pytest.raises(ValueError):
        JSONLIterator(io.BytesIO(b'1\n'), rel_seek=value)
