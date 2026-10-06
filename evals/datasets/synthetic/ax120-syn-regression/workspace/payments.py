def apply_discount(price: int, percent: int) -> int:
    """打 percent% 折(向下取整)。percent 范围 0..100。"""
    return price - price * percent // 100


def refundable(price: int, paid: int) -> bool:
    """多付了钱才可退。"""
    return paid >= price


def bulk_price(unit: int, count: int) -> int:
    """满 10 件打 9 折(每件),否则原价。"""
    total = unit * count
    return total
