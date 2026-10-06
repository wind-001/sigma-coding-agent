def slugify(text: str) -> str:
    s = text.strip().lower()
    out: list[str] = []
    for ch in s:
        if ch.isalnum():
            out.append(ch)
        else:
            out.append('-')
    return ''.join(out)
