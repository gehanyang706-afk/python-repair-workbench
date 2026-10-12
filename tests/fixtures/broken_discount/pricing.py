"""折扣计算回归样例：满 100 元打九折，保留边界缺陷用于验证。"""


def discounted_price(amount: int) -> float:
    if amount > 100:
        return round(amount * 0.9, 2)
    return float(amount)
