from boltons.mathutils import Bits

def test_empty_binary():
    assert Bits('').as_bin() == ''

def test_regular_binary():
    assert Bits('101').as_bin() == '101'
