import pytest
from cachetools import Cache, LRUCache, LFUCache, FIFOCache, RRCache

@pytest.mark.parametrize("kind", [Cache, LRUCache, LFUCache, FIFOCache, RRCache])
def test_growth_uses_additional_size_and_preserves_keys(kind):
    options = {"choice": lambda keys: sorted(keys)[0]} if kind is RRCache else {}
    cache = kind(maxsize=12, getsizeof=len, **options)
    cache["first"] = "abc"
    cache["second"] = "xx"
    cache["second"] = "123456789"
    assert set(cache) == {"first", "second"}
    assert cache.currsize == 12
    assert cache["first"] == "abc"


def test_eviction_of_replaced_key_still_accounts_full_size():
    cache = Cache(maxsize=10, getsizeof=lambda value: value)
    cache["first"] = 4
    cache["second"] = 5
    cache["first"] = 8
    assert dict(cache) == {"first": 8}
    assert cache.currsize == 8
    with pytest.raises(ValueError):
        cache["first"] = 11
    assert cache.currsize == 8
