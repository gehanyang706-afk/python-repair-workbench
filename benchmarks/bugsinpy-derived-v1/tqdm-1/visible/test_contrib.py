"""BugsInPy tqdm-1: original test_enumerate assertions, without nose fixtures."""
from io import StringIO
from tqdm import tqdm
from tqdm.contrib import tenumerate

tqdm.monitor_interval = 0


def test_enumerate():
    with StringIO() as our_file:
        a = range(9)
        assert list(tenumerate(a, file=our_file)) == list(enumerate(a))
        assert list(tenumerate(a, 42, file=our_file)) == list(enumerate(a, 42))
