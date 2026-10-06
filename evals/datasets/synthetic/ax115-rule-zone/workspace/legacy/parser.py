def parse_pair(text: str) -> tuple[str, int]:
    key, _, value = text.partition('=')
    return key.strip(), int(value)
