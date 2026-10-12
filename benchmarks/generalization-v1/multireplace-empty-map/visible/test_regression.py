from boltons.strutils import MultiReplace

def test_empty_map():
    assert MultiReplace({}).sub('hello') == 'hello'

def test_regular_map():
    assert MultiReplace({'cat':'dog'}).sub('cat nap') == 'dog nap'
