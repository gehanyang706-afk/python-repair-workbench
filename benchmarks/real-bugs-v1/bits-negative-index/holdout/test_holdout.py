from boltons.mathutils import Bits
import pytest

@pytest.mark.parametrize('text', ['1', '0', '10010', '11101'])
def test_negative_indices(text):
    bits = Bits(text)
    for offset in range(1, len(text)+1):
        assert bits[-offset] == (text[-offset] == '1')
    with pytest.raises(IndexError):
        bits[-len(text)-1]

def test_empty_index():
    with pytest.raises(IndexError):
        Bits('')[-1]
