# 데이터 출처와 공개 범위

## Olist

원천은 Olist가 공개한 [Brazilian E-Commerce](https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce)와
[Marketing Funnel](https://www.kaggle.com/datasets/olistbr/marketing-funnel-olist)이다.
수집 당시 카드에 표시된 라이선스는 CC BY-NC-SA 4.0이며, 원본 ZIP/CSV는 공개 저장소에 넣지 않는다.
수집된 바이트·열·건수·출처는 [intake-v1.json](../data/olist/intake-v1.json)에 기록했다.
계약과 집계·진단 증거는 원본 식별자나 리뷰 본문을 재배포하지 않는다.
Olist 파생 집계 자료의 이용에는 원천 라이선스 조건이 적용된다.

## NVIDIA Nemotron-Personas-Korea

[NVIDIA Corporation의 공개 합성 데이터](https://huggingface.co/datasets/nvidia/Nemotron-Personas-Korea),
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)를 사용했다.
직업·배경 다양성을 참고하기 위한 12개 seed와 출처·응답 해시만 선별했고,
별도의 Olist 질문을 작성했다. 출처 문장을 모델 학습에 사용하지 않았다.
[선별 seed와 상세 attribution](../data/olist/personas/nemotron-korea-seeds-v1.json).
전체 다운로드 응답은 로컬에 보존하며 Git에서 제외한다.
seed의 인물 서술은 합성 페르소나로 실제 고객 정보가 아니다.

## 코드·모델·실행 기록

프로젝트 자체 코드에는 별도 오픈소스 라이선스를 부여하지 않았다.
의존 패키지와 모델은 각 배포자의 라이선스·이용 조건을 따른다. 가중치는 포함하지 않는다.
공개 증거는 공개·합성 질문, 집계 결과, 모델 설정, 실패와 검사 결과다.
이전 실행 증거 일부에는 로컬 파일 위치와 실행 ID가 남아 있다. 이는 배포 주소가 아니다.
개인 자격 증명, 원본 데이터 행, 모델 캐시, 로컬 상태 디렉터리는 제외한다.
