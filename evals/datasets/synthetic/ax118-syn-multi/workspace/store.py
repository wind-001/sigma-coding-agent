"""用户存储:api 层从这里取用户。"""

def get_user(user_id: str) -> dict:
    db = {'u1': {'username': 'alice', 'email': 'a@x'},
          'u2': {'username': 'bob', 'email': 'b@x'}}
    user = dict(db[user_id])
    user['id'] = user_id
    return user
