import pytest
from boltons.strutils import MultiReplace

@pytest.mark.parametrize('text', ['', '你好\n世界', 'aaa', '123'])
def test_empty_map_various_inputs(text):
    assert MultiReplace({}).sub(text) == text

def test_empty_regex():
    assert MultiReplace({}, regex=True).sub('a.b') == 'a.b'

def test_nonempty_regex():
    assert MultiReplace({r'\d+':'N'}, regex=True).sub('a123b') == 'aNb'
