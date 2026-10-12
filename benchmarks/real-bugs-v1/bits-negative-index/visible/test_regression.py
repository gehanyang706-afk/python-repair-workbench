from boltons.mathutils import Bits
import pytest

def test_last_bit():
    assert Bits('1011')[-1] is True

def test_positive_index():
    assert Bits('1011')[1] is False
