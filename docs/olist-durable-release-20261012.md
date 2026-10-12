# Olist 영속 조사·AWS 배포 및 복구 검증 — 2026-10-12

운영 중 프로세스가 멈췄을 때 같은 질문을 다시 보내도 모델 호출 비용을 중복으로 쓰지 않고,
검증한 데이터와 이미 완료한 결과를 유지하는 경로를 구현했다. 공개 API 경계를 추가하고
실제 AWS에서 HTTPS, 장애 주입, S3 백업 복원과 복원본 API를 검증했다.
아래는 사용자 승인하에 조력자가 수행한 단기 검증 기록이며 고객 서비스 운영 실적이 아니다.

## 배포한 소스와 데이터

- 실행 이미지의 소스: `57ec671` (`git archive`로 만든 Linux ARM64 이미지).
- 이미지 digest: `sha256:9e3af0208c481a14332a576e365243f814027b74f122fce42575057644b7a645`.
- EC2/Caddy + 비공개 Single-AZ PostgreSQL RDS + 암호화 EBS/비공개 S3.
- 공개 Olist 데이터의 기존 게시 버전을 복사했다. 원본 로컬 DB를 클라우드 마이그레이션으로 변경하지 않았다.
- NVIDIA Super는 질문을 고정 작업 목록으로 분류한다. DB 행·SQL·연결 정보는 모델에 보내지 않는다.
- 복구 스크립트와 검증 영수증은 배포 이미지 검증 후 소스 저장소에 추가했다.

## 확인 결과

| 경계 | 실측 결과 | 공개 증거 |
|---|---|---|
| 회귀·실제 PostgreSQL | 731 passed, 0 skipped; Ruff 통과 | [pytest](../evidence/olist-durable-aws-20261012/pytest.txt), [Ruff](../evidence/olist-durable-aws-20261012/ruff.txt) |
| 로컬·AWS 실제 모델 | 미연결 계약 462/842, 유입 경로 누락 60/8,000을 독립 조회와 대조 | [로컬](../evidence/olist-durable-aws-20261012/local-http.json), [HTTPS](../evidence/olist-durable-aws-20261012/aws-http.json) |
| 멱등성·부모 재시작 | 동일 키의 조사 ID·결과 지문 유지, 추가 호출 예약 없음; 다른 본문은 409 | [재시작](../evidence/olist-durable-aws-20261012/aws-restart.json) |
| 공개 API | 미인증 401, Origin 403, 큰 본문 413, 잘못된 JSON 422, 초과 요청 429; 예산 불변 | [보안 경계](../evidence/olist-durable-aws-20261012/public-security.json) |
| 워커 강제 종료 | pending 시도 중 SIGKILL → 503 `worker_crashed`; 새 워커 준비 후 동일 키는 `attempted_unknown`, 추가 예약 없음 | [장애 주입](../evidence/olist-durable-aws-20261012/worker-fault.json) |
| PostgreSQL 복구 | pg_dump → S3 → 다운로드 지문 검증 → 같은 RDS의 별도 DB; 14개 테이블·뷰의 행 수와 정렬한 행 내용 지문 일치 | [DB 복원](../evidence/olist-durable-aws-20261012/postgres-recovery.json) |
| SQLite 복구 | S3 백업 무결성·전체 논리 내용 일치, 예약 4/32와 pending 1건 유지 | [조사 기록 복원](../evidence/olist-durable-aws-20261012/sqlite-restore.json) |
| 복원본 API | 복원 DB·SQLite에 연결한 과거 결과 재생과 실제 Super 신규 조사 2건 통과 | [재생](../evidence/olist-durable-aws-20261012/aws-restored-replay.json), [신규 조사](../evidence/olist-durable-aws-20261012/aws-restored-fresh.json) |
| 네트워크·권한·로그 | 공개 RDS/SSH 없음, 읽기 전용 TLS 연결, core 접근·마트 쓰기 차단; 검사한 로그에 키·질문 누출 없음 | [네트워크](../evidence/olist-durable-aws-20261012/network.json), [권한](../evidence/olist-durable-aws-20261012/runtime-verification.json), [로그](../evidence/olist-durable-aws-20261012/cloudwatch-secret-scan.json) |

최초 장애 주입 제어 스크립트는 워커 프로세스 이름을 잘못 찾아 종료하지 못했고, 그 요청은
정상 완료되어 호출 1회를 사용했다. 제어 대상을 수정한 다음 실제 pending 상태에서 워커를
종료했다. 이 이력을 지우거나 예산을 환급하지 않았다. AWS 전체 예약은 최종 6/32이며,
결과 미확정 예약 1건을 포함한다. 재생·충돌·인증 실패가 예산을 소모하지 않는지 별도 대조했다.

## 종료와 비용

이번에 만든 리소스·백업과 임시 API DNS 삭제를 완료했다. 21개 확인 항목이 모두 통과했다.
RDS 자동 스냅샷은 스택 삭제 후 잠시 조회되어 별도로 재확인한 뒤 부재를 확정했다.
[정리 영수증](../evidence/olist-durable-aws-20261012/cleanup.json)을 남겼고
기존 포트폴리오·다른 서비스는 보존했다.
승인 예산은 AWS USD 5였다. 리소스가 없어도 단기 사용 요금은 늦게 청구될 수 있으므로
리소스 삭제를 청구 금액 0원으로 표현하지 않는다. 상시 운영 API 주소는 제공하지 않는다.

## 이 결과가 보장하지 않는 것

- SQLite와 실행 잠금은 단일 호스트용이다. 분산 락·다중 인스턴스·자동 장애 조치가 아니다.
- 복원 대상은 같은 RDS 인스턴스의 별도 DB와 같은 EC2의 새 SQLite 경로다. 새 호스트/새 RDS
  인스턴스에서의 재난 복구, Multi-AZ 장애 조치, RPO/RTO는 이번에 검증하지 않았다.
- 복원은 실행을 중지한 최신 동일 백업으로 했다. 더 오래된 백업은 호출 예산을 환급할 위험이
  있어 자동 승격하지 않는다. 원격 완료 여부를 확인할 수 없는 호출도 자동 재전송하지 않는다.
- HTTP 두 질문의 숫자 일치는 일반 답변 품질 점수가 아니다. 모델 품질 평가는
  [별도 평가 기록](olist-boundary-fix.md)의 문항·범위를 유지한다.
- 외부 API exactly-once, 고객 트래픽 운영 실적, HA·SLA, 상시 알람·온콜은 주장하지 않는다.

구현·재실행 방법은 [영속 조사 서비스](olist-durable-service.md),
클라우드 작업 순서는 [AWS 운영 절차](aws-release.md)에 있다.
