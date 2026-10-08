# 결과·필수 검증·승격 판정

이 문서는 AC 6의 한국어 문서 검토다. AC 2~5의 수락된 구현 증거를 연결하고 현재
checkout에서 문서 명령과 전체 회귀를 별도로 검사한다. 실제 모델 추론은 AC 4 보존 결과를
검토한 것이며 이번 문서 검사에서 재추론하지 않았다. 모두 로컬 합성 데이터·조력자 수행이다.

## 세 판정을 구분한다

- 프로젝트 판정: **로컬 필수 검증 통과**. 수락된 선행 AC 증거와 이번 복사본 검증을 종합한 판정이다. 필수 범위: [일곱 산출물](README.md), 데이터 정확성·안전 경계·복구·기존 CLI/API 회귀 및 실제 실험 실행을 확인한다. 현재 문서 단계의 새 검사 결과는 아래 검증 기록에 남긴다. 최종 Seed 합격은 외부 검증기 판정이다.
- 에이전트 승격: **실패, 실험용 유지**. 모델 품질 0/3을 필수 안전 검사 통과와 혼동하지 않는다.
- 학습자 역량: 마케팅 단계 직접 실행·독립 설명은 **미입증**. 학습자 재실행을 새 합격 조건으로 추가하지 않는다.

실제 모델이 잘못 결론을 시도했어도 gate가 차단하고 실험이 실패를 정확히 기록하면,
그것은 모델 품질 실패다. gate 우회·근거 위조·복구 실패·실추론 미수행은 필수 검증 실패이며
에이전트가 실험용이라는 이유로 면제하지 않는다.

## 실제 비교 결과

모델 `mlx-community/Qwen3-4B-4bit`, revision `4dcb3d101c2a062e5c1d4bb173588c54ea6c4d25`,
prompt `marketing-agent.v1`, fixture `marketing-acceptance.v1`의 세 파생 시나리오다.
각 사례는 같은 계정/날짜/snapshot/manifest에서 기준선과 비교했다. 과거 fixture 시계를
사용했으며 현재 신선도 증거가 아니다. [원시 생성·관측·점수](../../evidence/marketing-ac4-experiment.json)를 보존한다.

| 사례 | 기준선 정확/완전/호출/초 | 실제 모델 정확/완전/호출/초 | 실제 종료 |
|---|---|---|---|
| observed_decline | 1/1/3/0.321 | 0/0/1/7.087 | evidence_reference → unknown |
| incomplete | 1/1/3/0.307 | 0/0/1/6.644 | evidence_incomplete → unknown |
| zero_spend | 1/1/3/0.294 | 0/0/1/6.462 | evidence_incomplete → unknown |

기준선 정확 3/3·완전 3/3·오류 0·정상 보류 2, 실제 모델 정확 0/3·완전 0/3·오류 3·
정상 보류 0·오류 unknown 3이다. 모델은 metrics를 선택한 뒤 quality/freshness 없이
finish했다. 게시된 미지원 주장은 0이나, 이는 모델이 올바르게 추론했다는 뜻이 아니다.
`unsupported_claim` 오류로 집계된 수는 0이며 실제 차단 오류는 evidence_reference 1건,
evidence_incomplete 2건이다. observed_decline에서는 발급되지 않은 증거 ID를 제출했다.

[사전 등록](../../data/marketing/experiments/preregistered-v1.json)은 최소 실제 사례 20,
정확·완전 각 100%, 오류·미지원 게시 주장 0, 기준선 대비 지연 2배 이하를 요구한다.
6개 gate 중 미지원 게시 주장 0만 통과했다. 이 수치는 조력자의 사전 등록 기본값이며
사용자 제공 수치가 아니다. 3개 고정 사례의 최종 실행만 점수화했고 앞선 재실행을 더해
독립 표본 수를 부풀리지 않았다. 이전 9 trials와 수정 후 3 trials, 총 12 trials의 경위는 [실험 기록](model-experiment.md)에 있다.
작은 합성 부분집합이므로 p95·실제 고객 품질·미지 운영 분포로 일반화하지 않는다.

## 필수 검증의 근거

| 경계 | 확인된 선행 증거 | 한계 |
|---|---|---|
| 데이터/보고 | [AC2](../../evidence/marketing-ac2-verification.json): 10,000원 중복 불변, 충돌 차단, backfill·원자 게시·freeze | completeness는 원천 선언, 로컬 filesystem |
| 진단 안전 | [AC3](../../evidence/marketing-ac3-verification.json): scope/injection/JSON·수치·증거·한도·timeout | 테스트 대체 worker는 실제 모델 증거 아님 |
| 실제 모델 | [AC4](../../evidence/marketing-ac4-verification.json): offline 실제 tool 선택, same snapshot 비교 | 품질 미달, 위 실제 3사례만 추론 |
| graph | [AC5](../../evidence/marketing-ac5-recovery.json): 18개 new-process/accept/reject/thread/concurrent/stale/crash/replay | 결정론적 그래프; 모델 checkpoint/외부 exactly-once 아님 |
| 문서와 기존 CLI/API | [현재 AC6 검토](../../evidence/marketing-ac6-docs.json), 아래 관찰자 명령 | 실제 모델/API 부하 재실행 아님; 사용자 포트 8000 미사용 |

문서 검토 중 선행 실험과 변경 코드의 SHA256이 달라 일시적으로 전체 판정을 보류했다.
이후 별도 검토 작업이 수정 코드로 실제 오프라인 3사례를 다시 실행한 증거를 제공했다.
2026-10-07T10:07:54Z 시작 실험의 model source hash는 현재 파일과 일치한다.
위 표는 그 새 증거이며, 이전 실험은 [수정 전 증거](../../evidence/marketing-ac4-experiment-before-review-repairs.json)에 보존돼 있다.
AC6는 실제 추론을 실행한 주체가 아니며 갱신된 선행 증거의 해시·출력·점수를 검토했다.
새 graph 증거도 지연된 완료 검토 replay를 포함해 18개 통과다.
프로젝트 필수 검증은 이 선행 증거와 현재 회귀를 함께 판정하며 에이전트는 실험용으로 유지한다.

첫 전체 검사에서는 문서의 현재 코드 인증 거절 1개, graph의 늦은 검토 replay 1개,
모델 프로세스 프레임/종료 경계 4개로 6 failed / 405 passed였다. 실행 중 별도 소스 변경으로
원본 지문 비교도 실패했다. 이 명령은 합격 명령에 포함하지 않는다. 첫 Ruff에서도 별도
review regression 테스트의 E501 1개가 있었다. 이번 AC는 이웃 AC 코드를 수정하지 않는다.
후속 전체 pytest -q는 412 passed였지만 다른 작업의 원본 변경으로 관찰자 종료는 실패했다.
이 역시 합격 명령에 포함하지 않는다. AC4의 범위 분리 관례에 따라 AC6 관찰자는 모든
검토 파일·모델 source hash·의존성 잠금만 감시한다. 전체 suite는 완전한 임시 복사본에서
실행하며 제외된 파일이 현재 원본과 계속 같다는 보장은 하지 않는다.
최종 AC6 관찰자에서 `uv run --locked pytest`와 `uv run --locked pytest -q`는 각각
414개 통과, 종료 코드 0이다. `uv run --locked ruff check .`도 종료 코드 0이다.
문서 전용 6개 검사에는 누락 파일·점수 과장·허위 승격·미검토 소스 변경 거절과 README
명령 그대로의 build/report/새 프로세스 reject 검증이 포함된다. 제품 성공 stderr는 비어 있다.
앞선 실패 이력을 삭제하지 않았으며 최종 검사와 구분한다. 정확한 명령과 검토 hash는
[AC6 기록](../../evidence/marketing-ac6-docs.json)에 있다.
기존 증거의 테스트 수는 해당 시점의 기록이다. 루트 status의 이전 279개 등 과거 수치를
현재 checkout의 테스트 수로 사용하지 않는다. 이전 기록의 Starlette/httpx deprecation 경고는
과거 결과이며 이번 최종 pytest 출력에는 경고가 없었다. uv의 환경 설치 안내 stderr는
제품 CLI 오류와 구분한다.

```sh
python3 scripts/verify_marketing_ac6.py -- uv run --locked python scripts/check_marketing_docs.py
python3 scripts/verify_marketing_ac6.py -- uv run --locked pytest -q tests/test_marketing_docs.py
python3 scripts/verify_marketing_ac6.py -- uv run --locked pytest
python3 scripts/verify_marketing_ac6.py -- uv run --locked pytest -q
python3 scripts/verify_marketing_ac6.py -- uv run --locked ruff check .
```

검증은 외부 임시 복사본에서 실행하고 AC6 감시 대상의 원본 지문 유지를 확인한다.
문서 checker는 증거 해시·수치·상대 링크·일곱 산출물을 검사하고 JSON을 출력한다.
체크 통과만으로 prose 의미 전체를 자동 증명하지 않는다. 한국어 주장 범위는 조력자가
브리프와 증거를 대조해 검토했다. 현재 AC6에서 안전 구현이나 모델을 바꾸지 않았다.

## 남은 운영·학습 범위

[AWS](aws-migration.md)는 선택적 설계만 있으며 배포·IAM 적용·과금·RPO/RTO 실측은 없다.
실제 원천 연동·인증·원천 control total 대조·분산 writer·상시 모니터링·대규모 성능은
추가 검증 대상이다. [포트폴리오 문구](portfolio.md)는 조력자 지원과 로컬 한계를 포함한다.

## 최종 통합 검사와 실행 방식 변경

[AC1 격리 실행 증거](../../evidence/marketing-ac1-release.json): 새 환경에서
locked pytest와 pytest -q 각각 419 passed, Ruff exit 0, 검사 stderr 없음,
검증 중 원본 지문 불변을 확인했다. 이 receipt는 실행 당시의 문서 해시도 보존한다.
이후 코드·테스트는 그대로이며 로컬 소비 한계·모델 프레임 제한·마감 상태 설명만
갱신했다. 변경된 문서의 현재 지문과 재검증은
[독립 검토 기록](../../evidence/marketing-main-review.json)에 별도로 남긴다.

사용자의 비용 절약 지시에 따라 자동 job을 6/7에서 종료했고 후속 formal evaluator와
Ralph는 실행하지 않았다. 남은 최종 판정은 gpt-6-luna/high의 제한된 읽기 전용 검토로
전환했다. 따라서 이 문서의 로컬 필수 검증 통과는 Ouroboros 정식 자동 승인과 다르다.
