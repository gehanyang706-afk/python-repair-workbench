"""Evaluator-only unknown/known total and scaling behavior."""
from io import StringIO
import re
import pytest
from tqdm import tqdm


@pytest.mark.parametrize('scale, expected', [(2, '14it'), (5, '35it'), (0.5, '3.5it')])
def test_unknown_total_scaling(scale, expected):
    with StringIO() as stream:
        for _ in tqdm((i for i in range(7)), file=stream, unit_scale=scale):
            pass
        assert expected in stream.getvalue()


@pytest.mark.parametrize('scale', [2, 5, 0.5])
def test_known_total_scaling(scale):
    result = tqdm.format_meter(2, 4, 1, unit_scale=scale)
    assert '50%' in result
    counts = re.search(r'([\d.]+)/([\d.]+)', result)
    assert counts is not None
    assert tuple(map(float, counts.groups())) == (2 * scale, 4 * scale)


@pytest.mark.parametrize('scale', [False, True, 1])
def test_default_unknown_total(scale):
    result = tqdm.format_meter(7, None, 1, unit_scale=scale)
    count = re.match(r'([\d.]+)it', result)
    assert count is not None and float(count.group(1)) == 7
