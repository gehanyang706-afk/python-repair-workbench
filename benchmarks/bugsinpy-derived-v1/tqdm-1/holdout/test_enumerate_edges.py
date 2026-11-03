"""Evaluator-only preservation and boundary cases; frozen before model runs."""
import pytest
from tqdm.contrib import tenumerate, tzip, tmap


@pytest.mark.parametrize('start', [-9, 0, 1, 73])
@pytest.mark.parametrize('values', [[], ['x'], ['a', 'b', 'c']])
def test_sequence_indices(start, values):
    assert list(tenumerate(values, start=start, disable=True)) == list(enumerate(values, start))


@pytest.mark.parametrize('start', [-2, 12])
def test_one_shot_iterator(start):
    assert list(tenumerate(iter(['p', 'q']), start, disable=True)) == [(start, 'p'), (start + 1, 'q')]


def test_other_wrappers_preserved():
    assert list(tzip([1, 2], [3], disable=True)) == [(1, 3)]
    assert list(tmap(lambda x: x * 2, [1, 2], disable=True)) == [2, 4]
