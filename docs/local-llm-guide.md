# LLM Ops Lab

공개 가중치 LLM의 추론을 이해하고, 데이터 파이프라인 운영 문서를 근거로 답하는
서비스까지 만들어 가는 학습 프로젝트입니다.

**현재 범위: 로컬 추론 CLI와 HTTP API, 문서 검증·출처 기록, 키워드 검색·보류, 평가 실행기, 실험용 근거 선택·인용 검증, 합성 마케팅 데이터 제품·진단·SQLite 그래프.**
검색에서 생성까지 연결한 기초 RAG 경로를 갖췄습니다. 답변 품질의 최종 평가,
다중 턴 대화, 외부 서비스 배포, 파인튜닝은 아직 완료하지 않았습니다.
모델 실행 성공과 답변의 정확성은 서로 다른 검증입니다.

## 시작

Apple Silicon Mac, Python 3.12, uv를 사용합니다. 프로젝트 폴더에서 실행하세요.

```sh
sh scripts/bootstrap.sh
uv run --locked llm-lab --prompt '데이터 파이프라인에서 멱등성이 필요한 이유를 두 문장으로 설명해 줘.'
```

최초 실행은 공개 모델 약 350MB를 내려받습니다. 모델 파일은 `work/huggingface/`,
각 실행 결과는 `runs/`에 보관합니다. 다운로드 이후 `--offline`으로 실행할 수 있습니다.
둘 다 Git에서 제외합니다. 원문 질문과 답변이 기록되므로 공개·합성 데이터만 사용하세요.

시작 모델은 `mlx-community/Qwen3-0.6B-4bit`, revision
`73e3e38d981303bc594367cd910ea6eb48349da8`입니다. 작은 모델로 추론 구조를 배우는
출발점이며 한국어·추론 품질을 보장하지 않습니다. 모델 선택은 이후 평가 결과로 재검토합니다.

## 읽는 순서

1. [이번 주 목표와 완료 기준](week-plan.md)
2. [첫 실습](day-01.md)
3. [추론 코드](../src/llm_ops_lab/cli.py): 입력 → 토큰 → 모델 → 생성 → 실행 기록
4. [확인된 상태](status.md)

## 로컬 HTTP 서비스

```sh
uv run --locked --extra local llm-lab-serve
```

캐시된 Qwen3-4B를 한 작업 프로세스에 로드하며 기본값은 `scoped-v3` 검색과 `baseline/direct` 답변입니다.
127.0.0.1:8000에서 `/health/live`, `/health/ready`, `/v1/answers`, `/metrics`를 제공합니다.
추론 1개와 대기 2개로 제한하고 기한 초과 시 작업 프로세스를 종료·회수한 뒤 복구합니다.
실제 과부하·작업 프로세스 종료·정지·서버 재시작을 검증했습니다. 답변 의미 정확성은 보장하지 않습니다.
[실행 명령·API 계약·운영 한계](day-11.md)를 확인하세요.

## 참고 문서로 답하기

```sh
uv run --locked llm-lab --offline \
  --context-file data/runbooks/idempotency.md \
  --prompt '데이터 파이프라인에서 멱등성이 필요한 이유를 두 문장으로 설명해 줘.' \
  --temperature 0 --max-tokens 256 --seed 42
```

UTF-8 일반 파일을 최대 16 KiB까지 읽습니다. 없는 파일·빈 문서·인코딩 오류·크기 초과는
모델 로딩 전에 거절하며, 원문 바이트의 SHA-256과 실제 전달 본문을 실행 JSON에 남깁니다.
자세한 설명과 코드 수정 과제는 [문서 파일 실습](day-02.md)에 있습니다.

## 질문에 맞는 문서 자동 선택

```sh
uv run --locked llm-lab --corpus-dir data/runbooks --search-only \
  --prompt '일시적인 API 오류의 재시도 간격과 최대 시도 횟수는?'
uv run --locked llm-lab --offline --corpus-dir data/runbooks \
  --prompt '일시적인 API 오류의 재시도 간격과 최대 시도 횟수는?' \
  --temperature 0 --max-tokens 256 --seed 42
```

검색 점수·상위 후보·문서 집합 해시를 기록하며 선택 근거가 약하거나 모호하면 모델을 호출하지 않습니다.
검색 점수는 정답 확률이 아닙니다. [검색 실습과 한계](day-03.md)를 확인하세요.

`--retriever scoped-v2`는 검색 표현 정제와 학습 자료/실제 실행 기록의 범위 판정을 추가합니다.
문서의 `scope.json` 계약과 내용 해시를 검증하므로 문서 변경 시 계약도 검토·갱신해야 합니다.
기존 개발 검색은 18/20→20/20, 추가 검증은 14/20→15/20이었으며 검색 누락은 남아 있습니다.
추가 검증셋도 수정 과정에서 개발에 사용됐고, 독립 테스트 성능은 아닙니다.
[설계·실제 생성 확인·한계](day-07.md)를 확인하세요. 생략 시 기존 `baseline`을 사용합니다.

`--retriever scoped-v3`는 검색 표현을 정규화하고 1위 문서에 없는 핵심 단어가 남으면 보류합니다.
기존 개발 20/20을 유지했고, 추가 검증은 15/20→20/20, 혼합 질문 대조셋은 19/24→24/24였습니다.
모두 개발에 사용한 질문이며 일반화 성능이 아닙니다. 복구된 5문항의 실제 4B 답변은 수동 검토에서
4/5만 필수 내용·근거를 충족했습니다. 새 표현 `상한`은 여전히 보류하며, 기본값은 바꾸지 않았습니다.
[설계·실패 사례·실행 명령](day-08.md)을 확인하세요.

## 고정 평가 질문 실행

```sh
uv run --locked python -m llm_ops_lab.evaluation --search-only
uv run --locked python -m llm_ops_lab.evaluation --offline \
  --temperature 0 --max-tokens 256 --seed 42
```

새 배치 폴더에 문서·질문 사본, 조건과 해시, 문항별 실행 기록, 요약과 수동 채점표를 저장합니다.
검색 선택·보류 일치는 자동 검사하며, 답변의 사실·근거·형식은 사람이 따로 검토합니다.
개발셋 평가이므로 미리 보지 않은 질문에서의 성능으로 해석하지 않습니다.
[평가 실습과 실패 복구](day-04.md)를 확인하세요.

`--prompt-variant grounded-v2`로 의미 보존 지시를 추가한 실험 후보도 비교할 수 있습니다.
후보는 개선과 퇴행이 함께 나타나 기본값으로 채택하지 않았습니다.
[프롬프트 비교 결과와 수동 채점](day-05.md)을 확인하세요. 기본값은 기존 `baseline`입니다.

4B와 scoped-v3를 고정한 `--prompt-variant grounded-v3` 비교에서는 재시도 제한의 인과 오류는
개선됐지만 기존 정답 3개가 퇴행했습니다. 개발 문항의 내용·근거 통과는 18/25→19/25,
두 문장 형식은 19/25→14/25였고 기본값으로 채택하지 않았습니다.
[인과관계 프롬프트 비교와 비채택 근거](day-09.md)를 확인하세요.

`--answer-mode evidence-v1`은 근거 문장을 선택한 뒤 선택된 문장만으로 답변을 만들고
인용 ID와 원문 위치를 검증합니다. 실제 40문항 평가에서 출력 답변의 원문 인용은 21/21 일치했지만,
내용·근거 충족은 보류/차단을 포함해 13/25였습니다. 인용 검증은 의미 정확성 검증이 아닙니다.
기본값은 `direct`를 유지합니다. [실험 결과와 내일 API 작업 순서](day-10.md)를 확인하세요.

모델 비교에서는 고정 개발셋의 필수 내용·근거 동시 충족이 0.6B의 4/15에서 4B의 10/15로
개선됐습니다. 다음 개발 후보는 `--model qwen3-4b`로 선택합니다. 기존 CLI 기본값은 0.6B입니다.
4B 최초 실행에는 약 2.28GB 다운로드가 필요하며, 캐시 후 `--offline`을 사용할 수 있습니다.
실습 환경에는 후보 모델을 내려받아 검증했습니다.
[모델 비교·비용·남은 오류](day-06.md)를 확인하세요. 이 결과는 서비스 정확도 보증이 아닙니다.

## 검증

```sh
uv run --locked pytest -q
uv run --locked ruff check .
```

단위 테스트는 입력 제한과 기록 보존을 확인합니다. 답변 품질·동시 요청·복구는 별도 실험이 필요합니다.

## macOS에서 실행 파일은 있지만 모듈을 못 찾을 때

`ModuleNotFoundError: No module named 'llm_ops_lab'`가 발생하면 먼저 확인합니다.

```sh
.venv/bin/python -v -c 'import llm_ops_lab' 2>&1 | rg 'Skipping hidden|ModuleNotFoundError'
```

`Skipping hidden .pth file`이 나타나면, Python이 설치 연결 파일의 macOS hidden 속성 때문에
파일을 무시하는 것입니다. 이 환경에서는 속성 제거 직후 다시 붙는 현상까지 관찰했습니다.

현재 실제 가상환경은 `work/python-env/`에 두고 `.venv`는 그곳을 가리키는 상대 심볼릭 링크입니다.
이 배치로 옮긴 뒤 새 프로세스에서도 import와 추론이 성공했습니다. 기존 `uv run` 명령은 그대로입니다.
새 체크아웃은 `sh scripts/bootstrap.sh`로 이 배치를 재현합니다. 이미 일반 `.venv` 디렉터리가
있으면 스크립트가 중단하며 기존 환경을 덮어쓰지 않습니다.

프로젝트 모듈은 editable 설치의 `.pth` 파일을 통해 `src/`와 연결됩니다.
디렉터리 이름 `.venv`와 개별 파일의 hidden 속성은 구분해야 합니다.
전체 디렉터리에 재귀적으로 속성을 바꾸거나 `PYTHONPATH`로 증상을 가리지 않습니다.
hidden 속성을 붙인 외부 동작의 주체는 확인되지 않았습니다. 현재 배치에서도 재발하면 이를 조사합니다.
[같은 증상의 CPython 이슈](https://github.com/python/cpython/issues/148121)

## 설계와 범위

- 먼저 하나의 실제 모델과 CLI를 연결하고, 다음 단계에서 검색과 API를 추가합니다.
- 학습용 MLX 추론은 Mac 호스트에서 실행합니다. Docker 안의 Linux CUDA 환경과 혼동하지 않습니다.
- 첫 릴리스는 읽기 전용 문서 질의입니다. AI가 운영 명령을 자동 실행하지 않습니다.
- 로컬 개발 → 재현 가능한 로컬 서비스 실행 → 별도 승인 후 외부 배포 순서입니다.
- 공개 서버나 실제 사용자 운영이 없으면 포트폴리오에 그 경험이 있는 것처럼 쓰지 않습니다.

## 참고

- [MLX LM 공식 저장소](https://github.com/ml-explore/mlx-lm)
- [모델 카드](https://huggingface.co/mlx-community/Qwen3-0.6B-4bit)
- [기본 모델 설명](https://huggingface.co/Qwen/Qwen3-0.6B)

모델 카드의 라이선스는 Apache-2.0입니다. 모델 가중치를 Git에 포함하지 않습니다.
프로젝트 자체 코드에는 별도 오픈소스 라이선스를 부여하지 않았습니다. 외부 자료의 라이선스는 각각 적용됩니다.


## Marketing & Commerce Data Reliability Lab

합성 광고비·주문·환불에서 ROAS 하락과 데이터 품질 문제를 구분하는 로컬 프로젝트입니다.
Bronze/Silver/Gold, 읽기 전용 진단, 실제 오프라인 Qwen 도구 선택 비교,
SQLite LangGraph 새 프로세스 재개까지의 증거와 실행법을 모았습니다.
[일곱 산출물·실행 예제](marketing/README.md), [결과·판정](marketing/results.md),
[직무·포트폴리오](marketing/portfolio.md), [선택 AWS 설계](marketing/aws-migration.md)를 확인하세요.

보존된 실제 모델 실험은 3개 사례 모두 증거 참조·완전성 검증에서 unknown 처리되어 **실험용 유지**입니다.
수정 코드의 실추론 재검증 증거를 반영했으며 전체 로컬 검증 판정은 위 결과 문서를 따릅니다.
필수 안전·복구 검증과 모델 승격은 별개입니다. 구현·검증은 조력자 수행이며,
학습자의 마케팅 단계 직접 입증·프로덕션 운영·AWS 배포를 주장하지 않습니다.
기존 `llm-lab` CLI와 `/v1/answers` 사용법은 위의 내용을 유지합니다.

원본 workspace를 변경하지 않는 검사(외부 임시 복사본 사용):

```sh
python3 scripts/verify_marketing_ac6.py -- uv run --locked python scripts/check_marketing_docs.py
python3 scripts/verify_marketing_ac6.py -- uv run --locked pytest -q
python3 scripts/verify_marketing_ac6.py -- uv run --locked ruff check .
```
