"""구문 검사는 통과하지만 언어 서버가 정의되지 않은 이름을 찾는 실습 파일."""

def greet(name):
    return f"Hello, {name}!"


def broken_total():
    return missing_amount + 10
