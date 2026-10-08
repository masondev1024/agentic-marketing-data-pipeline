# 실제 오프라인 진단 실험 v1 (AC4)

AC6 문서 검토: 수정 코드로 재실행한 2026-10-07T10:07:54Z 실험을 현재 표에 반영했다.
[현재 판정](results.md), [검토 해시](../../evidence/marketing-ac6-docs.json),
[수정 전 실험](../../evidence/marketing-ac4-experiment-before-review-repairs.json)을 구분한다.
아래 초기 실험의 실패·재실행 경위는 이력이며, 현재 source hash와 일치하는 새 증거를 사용한다.

권위 입력: `work/marketing-aws-implementation-brief.md` 4·6절. 이 문서는 AC4만 다룬다.
전체 프로젝트 합격 또는 graph 검증을 주장하지 않는다.

## 데이터 흐름과 실패 경계

동결된 acceptance fixture → Python/SQL 게시 → 불변 Snapshot → 두 개의 독립 실행이다.
결정론적 기준선은 metrics/quality/freshness를 조회한다. Qwen3-4B는 관측 내용을 받아
다음 JSON 도구 호출 또는 finish를 직접 선택한다. 기준선 결과를 모델에 주거나
모델 실패 뒤 기준선 답변으로 바꾸지 않는다. 양쪽 scope와 manifest SHA가 같다.
모델의 숫자·증거 참조·완전성은 기존 DiagnosticSession이 검증한다.
인과관계는 unknown이고 예산 변경 도구는 없다.

`agent.py`의 scripted callback은 프로토콜 단위 테스트 전용이며 결과에
`test_double=true`로 표시한다. 실제 실행은 `offline_model.py`가 소유한 단일
subprocess가 기존 `InferenceRuntime`을 offline 모드로 호출한다. 캐시 밖 다운로드와
원격 모델 코드는 사용하지 않는다. 기존 서비스나 localhost:8000은 건드리지 않는다.
호스트 잠금은 이 실험 실행기들 사이의 동시 추론을 막는다. 별도 서비스의 추론 요청까지
이 잠금으로 통제한다고 주장하지 않는다.

## 사전 등록과 독립 기대값

`data/marketing/experiments/preregistered-v1.json`은 첫 추론 전에 작성했다.
브리프에 수치 임계값이 없으므로 수치는 조력자가 선택한 보수적 engineering defaults다.
사용자가 지정한 값으로 표현하지 않는다. 기존 도구 상한은 늘리지 않는다.
진단 wall 30초, 도구 6회, action 8회, 입력 2048/출력 256 모델 토큰,
누적 모델 토큰 8192, JSON 재시도 1회다. 모델 로딩은 별도 120초 deadline이며
측정 지연에서 분리한다. 시간 초과 시 소유한 프로세스를 kill/wait한다.
테스트는 실제 잠든 자식 프로세스의 회수와 잠금 해제를 검증한다.

수용 기대값은 기존 `data/marketing/v1/acceptance-expected.json`의 독립 산술이다.
alpha 첫 날짜 ROAS는 53000/15000, 다음 날짜는 20000/10000이므로 관측 하락이다.
incomplete와 zero_spend는 unknown이다. 모델 결과로 기대값을 수정하지 않는다.
과거 fixture의 as_of를 양쪽 평가 시계로 사용하는 역사적 재현이며 현재 신선도 증거는 아니다.

승격 조건은 최소 실제 사례 20개, 정확·필수증거 완전 비율 100%, 오류·미지원 게시 주장 0,
기준선 대비 지연 2배 이하다. 이번 3개 한정 실험만으로는 승격할 수 없다.
표본은 자원 보호를 위한 명시적 부분집합이며 전체 시나리오의 실추론 커버리지나
통계적 일반화, p95를 주장하지 않는다. 재실험은 acceptance 결과를 숨기지 않고
새 버전으로 등록해야 한다.

## 실행과 증거 해석

저장소 루트에서 실행한다. 실제 추론은 기존 local extra가 설치된 환경과 pinned 캐시가 필요하다.
출력 경로는 매번 새 외부 임시 디렉터리를 선택한다.

```bash
python3 scripts/run_marketing_model_isolated.py /tmp/marketing-ac4-real-v1-final
python3 scripts/verify_marketing_ac4.py -- uv run --locked pytest -q
python3 scripts/verify_marketing_ac4.py -- uv run --locked pytest
python3 scripts/verify_marketing_ac4.py -- uv run --locked ruff check .
```

실험 실행기는 외부 복사본에서만 상태를 쓰며 원본의 실험 의존 파일 hash를 비교한다.
다른 AC가 소유한 graph/docs 변경은 실험 관찰자 비교에서 제외한다. 원본 파일을 실행 중
수정하지 않는다는 검사이며 외부 시스템 부작용 전반을 증명하는 sandbox는 아니다.
검토 후 소규모 JSON을 `evidence/marketing-ac4-experiment.json`에 보존한다.
각 항목에는 원시 모델 JSON 출력, 토큰 수, 지연, 도구 관측과 evidence ID, fixture/model
revision, registration/source hash, 동일 scope, 기준선 및 모델 점수가 있다.
데이터는 합성이고 개인정보·인증 정보는 포함하지 않는다.

오류로 unknown이 된 경우와 정상적인 안전 판단 유보(`status=abstained`)를 별도로 센다.
미지원 주장의 차단 횟수는 `unsupported_claim` 코드만 집계하므로 모든 종류의 실패나
잘못된 모델 시도를 의미하지 않는다. 원시 출력과 실제 오류 코드를 함께 검토해야 한다.
모델이 선택한 도구가 실제로 실행되었다는 사실은 유용한 진단을 완료했다는 뜻이 아니다.

## 요구사항 연결

각 항목은 짧은 증거 참조이며 브리프 원문을 변경하거나 복사하지 않는다.

| 요구사항 | 구현·증거 |
|---|---|
| 브리프 4: 실제 모델 선택 loop와 결정론적 비교 | `agent.py`, `model_worker.py`, 실제 experiment JSON |
| 브리프 4: fail closed·검증·test double 구분 | DiagnosticSession 재사용, `test_marketing_agent.py` |
| 브리프 6: 동결 수용 사례·동일 snapshot·정직한 지표 | preregistered-v1, acceptance-expected, JSON의 case scores |
| 브리프 6: 자원 보호·로컬 재현·관찰자 검증 | isolated runner, subprocess deadline/lock/reap 테스트 |

구현·검증 주체는 조력자다. 학습자가 직접 이 실험을 실행하거나 이해를 입증했다는
증거는 이번 실행에 없다. 로컬 합성 실험이며 실제 광고 운영·프로덕션 실적이 아니다.

## 검토 결과

| 사례 | 기준선 정답/호출/초 | 실제 모델 정답/호출/초 | 모델 종료 |
|---|---|---|---|
| observed_decline | 정답 / 3 / 0.321 | 오답 / 1 / 7.087 | evidence_reference |
| incomplete | 정답 / 3 / 0.307 | 오답 / 1 / 6.644 | evidence_incomplete |
| zero_spend | 정답 / 3 / 0.294 | 오답 / 1 / 6.462 | evidence_incomplete |

모델은 세 번 모두 metrics를 직접 선택하고, quality/freshness 없이 observed_decline을
제출했다. 수정 후 observed_decline은 발급되지 않은 증거 ID로 evidence_reference,
나머지 두 사례는 evidence_incomplete로 차단됐다. 미검증 결론 게시를 차단했고 최종 결론은 unknown이다. 정확 0/3, 필수증거
완전 0/3, 오류 3, 정상 판단 유보 0, 오류에 의한 unknown 3이다. 기준선은 정확 3/3,
완전 3/3, 오류 0, 정상 판단 유보 2다. 모델의 게시된 미지원 주장은 0이지만 이는
모델 품질이 아니라 검증 경계의 차단 결과다. 인과 주장은 게시하지 않았다.

승격 6개 gate 중 미지원 게시 주장 0 조건만 통과했다. 최소 표본·정확성·완전성·오류·
비용 대비 지연 조건은 실패했다. **에이전트 실험용 유지**. 실제 실험은 완료되었으며
모델 실패는 측정 결과다. 안전 검증 실패를 실험용 명칭으로 면제한 것이 아니다.

앞선 동일 prompt 실행에서도 같은 세 오류가 관측됐지만 다른 AC의 동시 파일 변경으로
관찰자 종료가 실패했다. 그 실행은 합격 명령에 포함하지 않았다. 감시 파일 범위를
실험 의존 파일로 제한한 재실행을 당시 검토 JSON의 근거로 사용했다.

누적 토큰의 다음 생성 예약을 보강한 뒤 세 사례를 한 번 더 실행했다. 동일한 세 오류를
확인했고 당시 source hash와 일치하는 실행을 보존했다. 수정 전 총 실제
추론 trial은 9개(세 고정 사례 × 3회)이나, 독립 사례 수나 승격 표본을 늘린 것으로
계산하지 않는다. prompt와 사전 등록 기대값은 변경하지 않았다.

전체 회귀의 최초 실패 두 건은 동시 작업 중인 graph AC에서 발생했다. 그 파일은
AC4에서 수정하지 않았다. 이후 전체 395개가 통과했고 AC4 관찰자도 종료 코드 0이다.
모든 회귀 테스트는 임시 복사본에서 실행한다. 관찰자 hash 범위는 AC4 소유 파일과
의존 입력으로 한정하여 타 AC의 동시 편집과 이 실행의 원본 변경을 혼동하지 않는다.
실제 모델 설정은 seed=42, temperature=0.0이며 생성 전용 모델 초기화 시간은 case별
model.load_seconds로 별도 기록한다.

검토 수정 후 모델 프레임 deadline·출력 크기·프로세스 자원 회수 경계를 보강한 코드에서
3개 실제 사례를 재실행한 선행 증거가 갱신됐다. AC6는 그 증거를 대조했고 이 표를 갱신했다.
총 12 trials이지만 독립 사례는 여전히 3개다. 현재 모델 소스 해시는 검토 JSON과 일치한다.
최신 전체 회귀 수와 판정은 [종합 결과](results.md)를 따른다.

모델 프로세스 통신은 요청 64KiB·응답 128KiB 프레임으로 제한한다. 쓰기 대기와
개행까지의 전체 응답 읽기가 하나의 monotonic deadline을 공유한다. 시작 도중
취소나 spawn 실패에서도 소유한 프로세스·파이프·잠금 파일을 정리한다.
이 경계는 `tests/test_marketing_review_regressions.py`의 실제 자식 프로세스 테스트로
검증하며, 해당 테스트의 가짜 워커를 실제 모델 품질 증거로 사용하지 않는다.
