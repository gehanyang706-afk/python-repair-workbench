from boltons.mathutils import Bits

def test_empty_hex():
    assert Bits('').as_hex() == ''

def test_empty_roundtrip():
    assert Bits.from_hex('').as_bin() == ''
    assert Bits.from_hex(b'').as_bin() == ''

def test_empty_bytes():
    assert Bits('').as_bytes() == b''
    assert Bits.from_bytes(b'').as_bin() == ''

def test_nonempty_roundtrip():
    assert Bits.from_hex('0F').as_int() == 15
