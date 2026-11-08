---
name: code-check
description: Python 또는 JSON 파일을 검사하거나 수정하는 요청에 사용한다.
---

# 파일 검사 절차
1. read_file로 원문을 읽는다.
2. check_file로 현재 파일의 Python 또는 JSON 구문을 검사한다.
3. 수정이 요청되었고 필요한 경우 write_file로 변경한다.
4. 수정 후 check_file로 다시 검사한다.
5. 구문 검사와 실제 프로그램 실행은 구분해서 보고한다. 이 도구는 프로그램을 실행하지 않는다.
