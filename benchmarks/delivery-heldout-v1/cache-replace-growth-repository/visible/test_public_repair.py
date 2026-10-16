from cachetools import Cache

def test_growth_that_fits_preserves_other_entries():
    cache = Cache(maxsize=10, getsizeof=lambda value: value)
    cache["a"] = 3
    cache["b"] = 4
    cache["b"] = 7
    assert dict(cache) == {"a": 3, "b": 7}
    assert cache.currsize == 10
