import re


def normalize_text(text: str) -> str:
    # offset 기반 청킹을 안정적으로 하기 위해 원문 노이즈를 먼저 정리한다.
    normalized = text.replace("\r\n", "\n").replace("\r", "\n") # 윈도우 줄바꿈과 맥/옛날 스타일 줄바꿈을 전부 \n으로 통일
    normalized = normalized.replace("\ufeff", "").replace("\u200b", "") # BOM 문자나 눈에 안보이는 공백을 제거
    normalized = normalized.replace("\u00a0", " ").replace("\t", " ") # 특수 공백과 탭을 일반 공백으로
    normalized = "\n".join(line.rstrip() for line in normalized.split("\n")) # 각 줄 끝의 불필요한 공백을 제거하고 다시 합침
    return re.sub(r"\n{3,}", "\n\n", normalized).strip() # 줄바꿈이 3개 이상 연속된 부분을 줄바꿈 2개로
