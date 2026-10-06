from textutil import top_k_words, word_frequencies


def test_freq() -> None:
    assert word_frequencies('a B a') == {'a': 2, 'b': 1}


def test_camel() -> None:
    assert __import__('textutil').camel_to_snake('HTTPServer') == 'http_server'
