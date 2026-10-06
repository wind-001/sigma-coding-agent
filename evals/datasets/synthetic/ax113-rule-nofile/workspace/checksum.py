def checksum(lines: list[str]) -> int:
    """每个字符串取首字符编码求和;空串跳过。"""
    total = 0
    for line in lines:
        total += ord(line[0])
    return total
