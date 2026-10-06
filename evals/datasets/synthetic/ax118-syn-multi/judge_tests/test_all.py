from api import describe


def test_describe_u1() -> None:
    assert describe('u1') == 'alice <a@x>'


def test_describe_u2() -> None:
    assert describe('u2') == 'bob <b@x>'


def test_store_keeps_id() -> None:
    from store import get_user
    assert get_user('u1')['id'] == 'u1'


def test_store_has_name_and_username() -> None:
    from store import get_user
    user = get_user('u1')
    assert user['name'] == 'alice' and user['username'] == 'alice'
