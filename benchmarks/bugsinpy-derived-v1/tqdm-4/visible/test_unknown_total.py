"""BugsInPy tqdm-4: original test_nototal assertion, without nose fixtures."""
from io import StringIO
from tqdm import tqdm

tqdm.monitor_interval = 0


def test_nototal():
    with StringIO() as our_file:
        for i in tqdm((i for i in range(10)), file=our_file, unit_scale=10):
            pass
        assert '100it' in our_file.getvalue()
