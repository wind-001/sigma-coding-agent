from textutil import camel_to_snake, top_k_words, word_frequencies


def test_freq() -> None:
    assert word_frequencies('a B a') == {'a': 2, 'b': 1}


def test_freq_punct() -> None:
    assert word_frequencies('go-go gophers!') == {'go': 2, 'gophers': 1}


def test_freq_digits() -> None:
    assert word_frequencies('v2 and v2') == {'v2': 2, 'and': 1}


def test_top_k_order() -> None:
    assert top_k_words('b a c a b', 2) == [('a', 2), ('b', 2)]


def test_top_k_tie_alpha() -> None:
    assert top_k_words('b a', 2) == [('a', 1), ('b', 1)]


def test_top_k_cut() -> None:
    assert top_k_words('a a a b', 1) == [('a', 3)]


def test_camel() -> None:
    assert camel_to_snake('HTTPServer') == 'http_server'


def test_camel_user_id() -> None:
    assert camel_to_snake('getUserID') == 'get_user_id'


def test_camel_simple() -> None:
    assert camel_to_snake('myVarName2') == 'my_var_name2'
