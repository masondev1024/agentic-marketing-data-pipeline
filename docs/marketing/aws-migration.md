# 선택적 AWS 마이그레이션 설계 — 미배포

현재 로컬 제품으로 요구사항을 검증한다. AWS는 공유 보고서·예약 batch·내구성 있는 운영
메타데이터가 실제로 필요해질 때의 선택 설계다. 리소스 생성·권한 적용·과금·배포는 하지 않았다.
실행 전 구체적인 리전·트래픽·예산·리소스 목록을 검토하고 별도 승인을 받아야 한다.
Databricks/PySpark/Delta 구현·마이그레이션과 Kubernetes는 범위 밖이다.

## 작은 batch부터 옮기는 순서

가정 예시: 계정 2개, 하루 원본 100MB, 하루 batch 1회, 보존 30일, 보고서 조회 하루 20회.
측정한 운영 수요가 아닌 비용·복구 설계를 설명하기 위한 가정이다.

1. S3에 `bronze/fixture_revision/...`, `snapshots/snapshot_id/...`, 별도 `quarantine/`을
   둔다. 먼저 컨테이너 batch에서 동일 Python/SQL 계약을 실행하고 모든 객체 SHA256을
   확인한다. Gold만 Glue Catalog에 명시적 schema로 등록해 Athena 소비를 선택적으로 제공한다.
   작은 데이터는 Glue ETL/Spark 없이 현재 DuckDB batch로 시작한다.
2. 파일 rename/flock을 S3에 그대로 이식하지 않는다. 불변 키는 If-None-Match로 만들고
   `CURRENT.json`은 읽은 ETag에 대한 If-Match 교체로 동시 publisher 충돌을 거절하는
   adapter가 필요하다. 전체 객체 게시 완료 뒤 포인터를 바꾸며, consumer는 snapshot ID를
   고정한다. S3 조건부 쓰기 기능은 [공식 설명](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html)을 따른다.
   이는 여러 객체를 묶은 transaction이 아니다. 409/412를 무조건 재시도하지 말고 현재
   manifest를 다시 읽어 충돌·중복 완료를 판별한다. ETag를 데이터 SHA256으로 간주하지 않는다.
3. 예약 batch는 필요 시 EventBridge에서 ECS 작업을 호출하고, 읽기 전용 보고 API는
   별도 컨테이너 역할로 분리한다. 광고 제어 API·임의 SQL 실행 endpoint는 추가하지 않는다.
   초기 동시 batch 1개, 소비자는 불변 snapshot 읽기로 제한한다. 인증·계정 권한 매핑은
   현재 호스트 scope와 별개의 미구현 경계다. 공개 서비스 전 별도 설계·검증이 필요하다.

## MLX·checkpoint 이식에서 바뀌는 부분

MLX는 현재 Apple Silicon 실행 경로다. 이를 일반 AWS Linux 컨테이너에 복사해 동일하게
실행된다고 가정하지 않는다. 1차 이전은 결정론적 보고·진단만 AWS에 두고 모델은 로컬
오프라인 실험으로 남긴다. 모델 제공까지 필요하면 호환 추론 엔진·가중치 형식·라이선스·
메모리/토큰 한도·동일 snapshot 품질·지연을 다시 평가한다. 엔진 변경을 기존 모델 실험과
동일 결과로 보지 않으며 현재는 새 모델/제공자/다운로드를 도입하지 않는다.

SQLite DB와 flock을 여러 ECS task의 임시 디스크나 공유 파일에 두지 않는다.
단일 호스트/EBS와 단일 실행자라면 경로 binding·DB 일관 백업·복원 시험을 먼저 해야 한다.
다중 replica가 필요하면 내구성 있는 PostgreSQL checkpoint 및 실행 원장으로 이전하는
별도 단계가 필요하다. RDS 등 후보의 비용이 있으므로 현재는 구성하지 않는다.
transaction으로 thread lease·proposal 중복 방지·예산 debit을 묶고 fencing으로 이전 소유자의
늦은 쓰기를 거절해야 한다. 기존 crash/replay·동시 재개 테스트를 해당 저장소에서 다시
통과하기 전 운영 가능 판정을 내리지 않는다. 외부 side effect exactly-once 보장은 없다.

## 권한·암호화·보존의 제안 경계

| 역할 | 필요한 최소 접근 설계 | 금지/분리 |
|---|---|---|
| 수집/batch | 입력 prefix 읽기, 새 snapshot·quarantine 쓰기, 검증 후 CURRENT 조건부 교체 | 광고 예산 권한·다른 환경 prefix·일반 삭제 권한 없음 |
| 보고/진단 | 승인된 Gold·정의·manifest 읽기, 필요 시 지정 Athena workgroup 쿼리 | Bronze/quarantine 직접 조회 및 데이터 게시 권한 없음 |
| checkpoint 서비스 | 해당 DB/schema에 필요한 transaction 권한 | 데이터 lake 게시·광고 제어 권한 없음 |
| 복구 관리자 | 별도 승인된 backup 복원·보존 종료 작업 | 일상 실행 role과 분리 |

실제 IAM policy/인증 정보는 만들지 않았다. 역할별 prefix와 계정 인증의 연결은 배포 전
권한 리뷰·교차 계정 음성 테스트로 검증해야 한다. TLS, public access 차단, 저장 암호화와
필요 시 SSE-KMS 키의 역할별 encrypt/decrypt 분리를 설계한다. 감사 로그에서 자격 증명과
원시 프롬프트를 제외한다. 예시 보존은 Bronze 30일, snapshot/manifest 90일, quarantine
7일, 운영 로그 14일이며 법적 보존 정책이 아닌 합성 데모 기본안이다. 진행 중 thread가
참조하는 snapshot은 만료시키지 않고 참조 해제 후 lifecycle 대상으로 삼는다.

Athena는 SELECT라도 결과 저장 위치가 생긴다. 지정 결과 prefix의 최소 쓰기 권한과 짧은
보존을 별도로 둔다. 모델에게 결과 버킷 쓰기 도구를 주는 것은 아니다.
[공식 결과 저장 설명](https://docs.aws.amazon.com/athena/latest/ug/querying.html)을 근거로,
데이터 lake 읽기 전용과 쿼리 서비스의 결과물 저장을 구분한다.

## 관측·복구·비용 검토

CloudWatch에 batch 성공/실패, schema 차단, oldest watermark 나이, 마지막 게시 시각,
읽기 오류·unknown 비율, checkpoint 충돌·예산 소진을 보낸다는 설계다. 현재 알림 배포는 없다.
실패 batch는 이전 포인터 유지 후 새 attempt로 제한 재시도하며 schema/conflict는 자동
재시도하지 않는다. 객체 백업과 DB의 일관된 recovery point를 연결해 격리 환경에서
복원하고 해시·scope·예산을 대조한 후 consumer 경로를 전환한다.
제안 목표 RPO 24시간/RTO 4시간은 미검증 목표이며 SLA나 달성 실적이 아니다.

예시 월량은 원본 3GB + 파생/버전 보존, batch 30회 × 실행 분,
조회 600회 × 쿼리당 스캔 bytes다. 월 비용은 S3 GB-month·요청 수 + Athena 스캔량 +
컨테이너 vCPU/메모리 시간 + DB/스토리지/백업 + 로그 수집/보존 + KMS + 전송량의 합으로
산정한다. Parquet 열 선택·날짜 partition·workgroup scan 한도로 쿼리 비용을 줄인다.
[Athena 공식 가격](https://aws.amazon.com/athena/pricing/)의 과금 모델을 참고하며,
리전별 단가를 확정하지 않았으므로 달러 총액이나 무료 운영을 주장하지 않는다.
소규모에서는 상시 DB·컨테이너·NAT/endpoint 고정 비용이 batch보다 클 수 있어
네트워크 경로까지 산정하고 로컬 유지를 먼저 비교한다.

운영 승인 시 월 예산과 알림 임계값을 정하고, 예산 알림이 자동 지출 차단을 보장하지 않음을
명시한다. 종료 계획은 스케줄 중단 → 소유한 task 종료 → 참조 확인·백업 검증 → 별도 승인된
리소스 삭제 순이다. 버전 객체·미완료 multipart·로그·EBS snapshot·DB backup·이미지 등
잔존 비용을 확인한다. 이 문서는 삭제 실행이나 AWS 프로비저닝 승인이 아니다.
