# Olist 요청·재사용·불확실한 호출의 로컬 검증

이 명령은 실제 로컬 API의 ASGI 처리, Supervisor 자식 프로세스, 워커의
`process_request`, durable graph와 SQLite 원장을 함께 통과한다. 제공자와 데이터
조회 세션만 결정적인 합성 fixture로 바꾼다. 소켓을 열거나 실제 모델·PostgreSQL·AWS를
호출하지 않는다. 운영용 워커 진입점의 모델 로딩·stdout 설정·runtime 종료는 제외된다.

## 기존 동작과 이번 보완

- 원장은 제공자 호출 전에 호출 슬롯을 예약한다. 완료 결과는 같은 principal/key와
  동일한 소스·모델·질문 계약일 때 재사용한다.
- 제공자 timeout/전송 결과를 확정하지 못하면 `503 attempted_unknown`으로 남긴다.
  재요청으로 자동 재전송하거나 예약을 환급하지 않는다.
- Supervisor의 제한 시간이 지나면 `504 inference_deadline_exceeded`를 반환하고
  기존 워커 종료를 기다린 뒤 새 워커를 만든다. 같은 키의 재요청은 원장의 pending
  시도 때문에 `503 attempted_unknown`이다.
- 이번 변경은 위 처리 방식을 바꾸지 않는다. 흩어진 기존 테스트를 대체하지 않고,
  HTTP 시도·논리 작업·모의 제공자 호출·원장 상태·예약을 한 영수증에서 대조한다.

확정되지 않은 호출을 다시 보내지 않으면 중복 실행 위험을 줄일 수 있다. 대신 실제로
실행되지 않은 호출도 pending 예약으로 남을 수 있어 사람이 제공자 결과를 확인해야 한다.
이는 외부 API의 exactly-once나 자동 복구를 보장한다는 뜻이 아니다.

## 재실행

이미 설치된 프로젝트 환경에서 실행한다. 아래 옵션은 잠금 파일을 유지하고 새 설치와
네트워크 의존성 조회를 막는다. 환경이 없다면 먼저 별도로 의존성을 검토해야 한다.

```bash
export UV_OFFLINE=1 UV_NO_SYNC=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src
export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
unset OLIST_TEST_ADMIN_DSN
mkdir -p work/local-validation
uv run --locked python scripts/validate_durable_local.py \
  --output work/local-validation/receipt.json
uv run --locked pytest -q -p no:cacheprovider tests/test_validate_durable_local.py \
  tests/test_olist_durable_graph.py tests/test_public_api_integration.py \
  tests/test_service.py tests/test_verify_durable_http.py
uv run --locked pytest -q -p no:cacheprovider
uv run --locked ruff check .
```

`--output`은 기존 파일을 덮어쓰지 않는다. 재실행 시 새 출력 이름을 사용하거나
옵션을 빼고 stdout의 JSON을 읽는다. 매번 독립 임시 디렉터리를 사용하고 종료 시 정리한다.
요청은 순서대로 한 개씩만 보내며 워커는 하나다. 임시 워커·데이터·원장은 실제 사용자
데이터와 분리되어 있다. 고정된 합성 질문과 3개 리드/1개 계약의 작은 집계를 사용하고,
공유하는 영수증에는 질문·프롬프트·토큰·DSN·개별 데이터 행을 넣지 않는다.

## 2026-10-25 관측 예시

아래는 assistant가 실행한 한 번의 합성 실험이다. 정상 제공자는 0.03초 기다린다.
워커 강제 제한은 2초이며 해당 fixture는 응답하지 않고 기다린다.

| 단계 | HTTP 결과 | 누적 작업 | 누적 모의 호출 | 누적 예약 | pending 시도 |
|---|---|---:|---:|---:|---:|
| 정상 | 200 completed | 1 | 1 | 1 | 0 |
| 완료 재사용 | 200 replayed | 1 | 1 | 1 | 0 |
| 제공자 timeout | 503 attempted_unknown | 2 | 2 | 2 | 1 |
| 같은 timeout 키 재요청 | 503 attempted_unknown | 2 | 2 | 2 | 1 |
| 워커 제한 시간 초과 | 504 inference_deadline_exceeded | 3 | 3 | 3 | 2 |
| 같은 중단 키 재요청 | 503 attempted_unknown | 3 | 3 | 3 | 2 |
| 새 키로 정상 요청 | 200 completed | 4 | 4 | 4 | 2 |

오류 응답은 주입한 장애와 안전한 재요청 거절을 포함해 HTTP 7회 중 4회다.
새 논리 작업 4개 중 2개는 완료, 2개는 미확정으로 남았다. 모의 제공자 timeout
이벤트는 호출 4회 중 1회, Supervisor 강제 제한은 HTTP 7회 중 1회다.

`accepted=7`, `completed=3`, `timeouts=1`은 기존 서비스 카운터다. `completed`에는
완료 재사용이 포함되며, 제공자 timeout은 `attempted_unknown`이므로 여기서는
`timeouts`에 더해지지 않는다. 결과의 `usage`와 `model_executed`는 원래 작업의
이력이라 재사용 응답을 합산해 실제 호출 수로 계산하면 안 된다.

지연은 각 HTTP 호출 직전부터 응답을 읽을 때까지 직접 측정한다. JSON에 각 샘플을
보관하고 빠진 메트릭은 0으로 대체하지 않는다. 7개 표본에 대한 서비스 p95, 처리량,
실제 모델 성능·청구 비용은 주장하지 않는다. 예약은 호출 슬롯이며 비용이 아니다.
실제 과금·실제 제공자 사용량은 이 실험으로 알 수 없다.

기존 테스트의 예약 직후/응답 직후 중단, 결과 저장 뒤 재사용, 데이터 조회 중단,
취소된 대기 요청과 활성 워커 용량 유지 검증도 함께 재사용한다. 예약 직후 중단은
호출 0회·예약 1개일 수 있으므로 일반적으로 예약 수와 호출 수가 같다고 가정하지 않는다.

[2026-10-12 AWS 기록](olist-durable-release-20261012.md)은 과거 참고 자료다.
오늘 변경을 AWS에서 실행했다는 증거로 사용하지 않는다. 동시성 부하, 다중 인스턴스,
HA, PostgreSQL 전환, 대시보드와 GitOps는 이번 검증 범위에 없다.
