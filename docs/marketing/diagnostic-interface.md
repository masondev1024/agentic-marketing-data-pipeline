# AC3: 읽기 전용 진단 인터페이스 v1

권위 입력은 `AGENTS.md`, `work/marketing-aws-implementation-brief.md`의 3번
Harness와 6번 공격 사례, 현재 Seed AC3이다. 브리프 원문은 수정하지 않았다.
이번 산출물은 **조력자가 구현·검증한 로컬 진단 경계**다. 모델 실행·평가와
LangGraph 재개는 다른 AC이며 이 문서에서 완료를 주장하지 않는다.

## 데이터 흐름과 신뢰 경계

호스트가 게시 디렉터리와 `Scope(snapshot_id, account_id, date_start, date_end)`를
선택한다. 이것은 로컬 호출자의 범위 고정이며 인터넷 서비스의 사용자 인증을
대신하지 않는다. 모델에는 경로·scope를 바꾸는 매개변수가 제공되지 않는다.
`CURRENT.json` → manifest SHA256 → 두 입력 artifact의 SHA256을 확인하고,
Gold의 계정별 지표와 버전별 정의만 읽는다. 지표 schema·키·금액·비율도 검증한다.
스냅샷 SHA는 콘텐츠 무결성 확인이며 악의적인 로컬 저장소 소유자에 대한 전자서명이 아니다.

선택 계정·기간의 지표를 불변 bytes로 캡처한다. 이후 도구는 이 bytes만 읽으며
게시 디렉터리나 Parquet를 다시 읽지 않는다. 따라서 도중의 새 게시가 기존
관측을 섞지 않는다. 기존 세션은 명시된 과거 snapshot을 유지하고, 신규 세션이
현재 게시와 다른 ID를 요청하면 `stale_snapshot`으로 거절한다.
시계는 기본 UTC 현재 시간이며 `--now`는 호스트의 합성 fixture 재현 옵션이다.

조회는 별도 단명 프로세스의 DuckDB 메모리 DB에서 고정 SELECT로 실행한다.
계정·날짜·행 한도는 SQL parameter로 전달한다. 외부 접근과 spill을 끄고
메모리 128MB, 스레드 1개를 사용한다. 시스템 SQL/셸/네트워크나 저장 도구는 없다.
호스트가 자기 워커를 시작·종료하는 것은 모델에게 셸 권한을 주는 것과 다르다.
OS 전체 sandbox는 아니며 신뢰된 설치 코드만 실행한다.

| 도구 | 출력과 목적 |
|---|---|
| `metrics` | 범위 내 일별 금액·주문수·ROAS. 비교의 숫자 근거 |
| `quality` | 범위 내 날짜별 complete. 원천의 완전성 선언이며 독립 감사는 아님 |
| `freshness` | as-of와 해당 범위의 날짜별 watermark |
| `definitions` | 고정 지표·환불·합성 귀속 정의 |

SQL 결과는 캡처한 입력과 별도로 대조하고 schema를 확인한다. 결과에 다른 계정,
잘린 날짜, 틀린 금액 등이 있으면 증거로 발급하지 않는다. 증거 ID는 버전·scope·
manifest hash·fixture revision·도구·출력을 묶은 SHA256이다. 반환 dict를 수정해도
세션 내부의 bytes는 바뀌지 않는다. 다른 scope 세션의 증거는 수용하지 않는다.
원천 캠페인 이름 같은 자유 텍스트는 도구 프로토콜에 전달하지 않는다.

## 실행 한도와 기본값의 출처

브리프는 시간·토큰·도구·행·반복 한도의 **종류**를 요구하지만 숫자는 지정하지
않았다. 다음 숫자는 이번 구현이 선택한 보수적 기본값으로, 사용자 확정 수치나
모델 승격 기준이 아니다. 호출자는 줄일 수만 있다. 신규 외부 제공자나 패키지는 없다.

| 항목 | 기본 상한 |
|---|---:|
| 단일 계정 / 기간 / 행 | 1계정 / 31일 / 31행 |
| 세션 전체 wall time / 도구 wall time | 30초 / 5초 |
| JSON action 수 / 실제 tool 실행 수 | 8 / 6 |
| 같은 도구 재호출 / 잘못된 JSON 재시도 | 즉시 종료 / 0회 |
| 단일 action UTF-8 / 성공 응답 envelope | 4,096 / 16,384 bytes |
| 누적 프로토콜 token units | 32,768 |
| snapshot manifest / 각 읽는 artifact | 1MiB / 8MiB |
| CLI JSONL 입력 | 32KiB, 1~8 action |

token units는 action 및 응답 전체의 UTF-8 bytes에 기반한 보수적 debit이며 실제
모델 토큰 측정치가 아니다. byte-level tokenizer의 protocol token 수를 상계한다.
카운터 자리수 증가에도 여유를 더한다. 종료 오류는 별도의 작은 제어 응답이다.
향후 inference 호출자는 시스템 prompt·생성의 실제 tokenizer 한도를 별도로
적용해야 한다. 이 AC에서는 모델을 실행하지 않는다.

입력 오류·반복·예산 소진은 terminal이다. 오류 뒤 호출로 예산을 초기화할 수 없다.
각 세션은 동시 dispatch를 `session_busy`로 거절한다. 취소 또는 시간 초과는
해당 자식만 kill/reap한 뒤 반환한다. 조회 결과가 행 한도를 넘으면 부분 지표를
내보내지 않고 종료한다. 워커 stderr·비정상 종료·잘못된 JSON도 오류로 처리한다.

## 요청·응답과 결론 검증

아래 tool 요청의 세 필드는 정확히 필요하다. `parameters`는 빈 object만 허용한다.
추가 field, 임의 SQL, 계정·기간 변경, tool 이름으로 가장한 injection은 실행 전에 거절한다.

```json
{"action":"tool","tool":"metrics","parameters":{}}
```

`DiagnosticSession.dispatch()`는 엄격 JSON parser(중복 키·NaN 금지), 정확한
필드·타입·enum 검사를 수행한다. `Scope`, `Limits`, `Snapshot`은 frozen dataclass다.
성공 관측은 `status`, `evidence_id`, `evidence`, `usage`를 반환한다.

```json
{"action":"finish","conclusion":"unknown","evidence_ids":[],"claims":[]}
```

finish는 네 필드만 허용한다. 결론 enum은 `unknown`, `observed_decline`,
`no_observed_decline`이다. 숫자 claim은 `{evidence_id,row,field,value}` 형식이며
발급된 metrics의 실제 row/field/value 및 타입과 정확히 일치해야 한다.
존재하지 않는 사실, 자유 서술형 원인·권고·예산 변경 필드는 거절한다.

관측 비교에는 metrics·quality·freshness 증거가 모두 필요하다. 정확히 두 날짜의
ROAS를 코드가 비교한다. 불완전 데이터, 0 지출, 과거/미래 시계 불일치,
as-of 또는 watermark 나이 86,400초 초과는 `unknown`으로 보류한다.
검증된 금액은 관측값으로 남으며 불완전 데이터에서 확정 성과로 승격하지 않는다.
모든 최종 결과는 `causality: unknown`, 빈 hypotheses와 빈 proposed_actions를 갖는다.
이는 마케팅 성과 변화의 관측이며 원인 식별이나 예산 변경 기능이 아니다.

## 실행과 실패 처리

기존 build/report, `llm-lab`, `/v1/answers`는 유지한다. Python 사용 예:

```python
from pathlib import Path
from llm_ops_lab.marketing.diagnostics import DiagnosticSession, Scope, Snapshot

scope = Scope(snapshot_id, "alpha", "2026-09-20", "2026-09-21")
session = DiagnosticSession(Snapshot.open(Path(product_directory), scope))
observation = session.dispatch('{"action":"tool","tool":"metrics","parameters":{}}')
```

추가 CLI(각 path와 ID는 호스트가 직접 선택):

```sh
uv run --locked python -m llm_ops_lab.marketing diagnose \
  --output-dir "$PRODUCT_DIR" --snapshot-id "$SNAPSHOT_ID" \
  --account-id alpha --date-start 2026-09-20 --date-end 2026-09-21 \
  --requests "$REQUESTS_JSONL" --now 2026-09-23T12:00:00Z
```

성공은 stdout JSON array, stderr 없음, 종료 코드 0이다. 실패는 구조화된
`error.code/detail`을 stderr로 출력하며 종료 코드 1이다. argv 파싱 오류는 argparse의
stderr와 코드 2이다. 잘못된 JSON/권한 오류를 정상 종료나 성공 관측으로 처리하지 않는다.
오류를 수정한 뒤 호스트가 새 세션을 만든다. 부분 응답을 근거로 계속하지 않는다.

## AC3 검증 대응

각 항목은 브리프 참조와 실제 검사명을 연결하며 2,000자 미만이다.

| 요구사항 / 출처 | 구현·검증 |
|---|---|
| 브리프 3: 불변 scope·snapshot·증거 | `Snapshot.open`, `Scope`; scope/date 침범·게시 교체·타계정 증거·반환값 변조 테스트 |
| 브리프 3,6: allowlist·injection·JSON | `dispatch`, `strict_json`; 비허용 tool·SQL/셸/네트워크·추가 field·원천 이름 injection을 거절/제외 |
| 브리프 3: 실행 한도·취소·no progress | `Limits`, `execute_bounded`; action/tool/token/output/row/wall 한도, 반복, 동시 호출, 실제 자식 hang/취소/회수 |
| 브리프 3,4의 인터페이스 경계: 근거 없는 주장 금지 | `_finish`; 숫자·증거 ID·타입 대조, 불완전·stale·0 지출 보류, 인과·예산 변경 거절. 모델 loop 구현은 이 AC 밖 |
| Seed AC3: 실패 CLI 계약 | 새 `diagnose` CLI의 stderr·비정상 코드, 정상 stderr 없음, 제품 파일 bytes 불변 검사 |

`tests/test_marketing_diagnostics.py`는 별도로 동결된 기존 acceptance 입력/수작업
기대값을 그대로 읽는다. 기대값을 구현 출력으로 다시 생성하지 않았다.
프로토콜 실패 테스트의 대체 워커는 명시적 test double이다. 실제 LLM 추론,
학습자의 직접 수행, 프로덕션 운영 증거로 제시하지 않는다.

수용 검증은 기존 격리 observer를 재사용한다(파일명의 AC2와 달리 command 전달 가능).
원본을 임시 외부 복사본으로 복제하고 uv 환경·캐시·런타임 산출물을 그곳에 만든 뒤
원본 파일 해시가 같은지 확인한다. 사용자 localhost:8000이나 모델 워커는 사용하지 않는다.

```sh
python3 scripts/verify_marketing_ac2.py -- uv run --locked pytest -q tests/test_marketing_diagnostics.py
python3 scripts/verify_marketing_ac2.py -- uv run --locked pytest
python3 scripts/verify_marketing_ac2.py -- uv run --locked pytest -q
python3 scripts/verify_marketing_ac2.py -- uv run --locked ruff check .
```

## 로컬 소비 한계와 수치 정밀도

계정별 31행을 반환해도 캡처 단계에서는 모든 캠페인·계정이 들어 있는
`gold/metrics.json`을 읽는다. 이 파일의 8MiB 한도를 넘으면 조회를 거절한다.
생산자가 허용하는 최대 행 수 전체를 이 소비 경로에서 처리했다고 검증하지 않았다.
규모를 늘릴 때는 계정별 artifact 또는 partition manifest로 조회 입력을 분리해야 한다.

quality/freshness 도구는 고정한 snapshot의 상태만 보여 준다. 이후 게시 시도의 실패
이력을 조회하지 않으므로 최신 파이프라인 전체가 정상이라는 뜻은 아니다. 운영자는
별도 report와 quarantine 실패 기록을 함께 확인해야 한다.

ROAS 표시값은 SQL DOUBLE과 같은 이진 부동소수점 표현이다. 결론은 표시값끼리
비교하지 않고 순귀속매출과 지출 정수의 교차곱으로 판정한다. 큰 금액에서 반올림된
ROAS가 같아도 원금액의 하락을 보존하며, freshness·완전성·0 지출 gate는 그대로 적용한다.
