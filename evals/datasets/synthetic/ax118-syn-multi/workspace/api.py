from formatter import format_user
from store import get_user


def describe(user_id: str) -> str:
    return format_user(get_user(user_id))
