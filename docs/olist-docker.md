# Olist PostgreSQL 로컬 운영

선택: A안의 로컬 PostgreSQL을 Docker Compose로 운영한다. AWS는 아직 배포하지 않는다.
PostgreSQL 17.11 공식 이미지는 digest로, Python 드라이버는 uv.lock으로 고정한다.
컨테이너는 localhost:55432, 전용 named volume, 메모리 상한 1GiB, CPU 1개를 사용한다.
호스트 8000의 기존 LLM API와 별개이며 진단 CLI를 해당 API에 연결하지 않았다.

조력자 로컬 검증: 실제 DB 통합을 포함한 460개 테스트와 Ruff 통과. 실제 데이터 진단,
동일 버전 재사용, 재시작, 별도 DB 복원의 전체 10개 테이블 행 해시/건수 일치를 확인했다.
[검증 증거](../evidence/olist-local-v1.json), [진단 결과](../evidence/olist-diagnosis-v1.json).

## 저장·계정 구조

- 원본: 기존 ZIP/CSV를 수정하지 않는다. 11개 CSV와 두 ZIP, 재실행용 manifest를 SHA256 고정 Bronze에도 보존한다.
- `olist_core`: 리드 `mql_id`, 계약 `mql_id`/판매자 유일성, 주문 `order_id`,
  주문상품 `(order_id, order_item_id)`, 커머스 판매자 `seller_id`. 키는 `version_id` 내에서 유일하다.
- `olist_mart`: 계약 판매자별 표본 주문과 유입 채널별 리드/계약/연결 건수.
- `olist_ops`: 원본·계약·변환 버전, 품질 결과, 실행 시도, 현재 게시 버전.
- 적재 `olist_writer`는 superuser가 아니다. 진단 `olist_reader`는 mart SELECT만 가능하다.
  `default_transaction_read_only`뿐 아니라 GRANT로 쓰기와 core/ops 직접 조회를 차단한다.
- 자격 증명은 Git 제외 `work/olist/secrets/`의 0600 파일이며 출력/증거에 기록하지 않는다.
  Compose secrets는 로컬 파일 마운트이며 클라우드 비밀 관리 서비스가 아니다.

## 첫 마트의 지표 경계

리드 8,000, 계약 842, 연결 판매자 380, 미연결 462는 고정 원본에서의 관측값이다.
원천이 바뀌면 새로운 계약/해시 검토 대상이며 이 수치를 영구 업무 규칙으로 하드코딩하지 않는다.
미연결 판매자의 주문수는 NULL과 `unlinked_unknown`으로 보존한다. 유입 경로 NULL도 분모에 포함한다.
주문 수는 판매자별 DISTINCT order_id이며 여러 상품 때문에 늘어나지 않는다. 여러 판매자의 주문수를
합치면 같은 주문이 여러 번 포함될 수 있으므로 회사 전체 유일 주문수로 해석하지 않는다.
주문 상태는 원문을 보존하며 첫 마트는 취소 등을 포함한 관측 주문수다. 완료 주문/매출이 아니다.

원본 금액은 통화 미확정 `numeric(18,2)`로 보존하며 금융 지표를 게시하지 않는다.
timestamp는 원본 시계의 naive 값이다. timezone/cross-source clock 해석이 해결되지 않았으므로
계약 후 30일 실적·전환 소요일·광고 귀속·ROAS·CAC를 제공하지 않는다. 첫 마트는 표본 전체
관측 주문과 연결 범위를 제공한다. 별도 `*_source_clock` 열은 원문 시계값으로 계약 전/후를
비교한 조건부 관측이며 `clock_basis=naive_source_clock_unverified`를 함께 저장한다.
이 비교를 시간대 검증을 마친 실제 계약 귀속 지표로 해석하지 않는다. 날짜 최댓값은 완전성 watermark가 아니다.

## 시작

Mac에서는 Docker VM 메모리도 별도로 소비된다. Docker Desktop 메모리 설정과 macOS 압력을
확인한다. 컨테이너 1GiB 상한은 Docker VM 전체를 1GiB로 줄이지 않는다.
이 프로젝트의 컨테이너는 자동 재시작하지 않아 Docker 실행 시 다른 작업과 함께 갑자기 켜지지 않는다.

저장소 루트에서:

```bash
uv run --locked python scripts/olist_local_setup.py
docker compose -f compose.olist.yaml config --quiet
docker compose -f compose.olist.yaml up -d --wait postgres
uv run --locked python -m llm_ops_lab.olist migrate
```

초기화 스크립트는 빈 볼륨에서만 계정을 만든다. 기존 볼륨에서 암호 파일을 재생성하거나
POSTGRES 환경 변수를 바꿔도 DB 암호는 변경되지 않는다. setup은 기존 암호를 덮어쓰지 않는다.
초기 역할 생성 도중 중단됐다면 로그로 원인을 확인한 후, 볼륨을 지우지 않고 다음 SQL을
재실행할 수 있다. 이미 존재하는 역할의 암호는 변경하지 않는다:

```bash
docker compose -f compose.olist.yaml exec -T postgres \
  psql -U postgres -d olist -f /docker-entrypoint-initdb.d/10-roles.sql
```

2026-10-07 원본 ZIP은 macOS dataless 상태/읽기 시간 초과로 별도 경로에 재확보했다.
두 ZIP 모두 원래 SHA256과 일치하며 기존 파일은 보존했다. 이 작업 환경에서는 다음을 사용한다:

```bash
uv run --locked python -m llm_ops_lab.olist validate \
  --intake work/olist-intake-recovery.nosync/intake.json \
  --output runs/olist/raw-validation.json
uv run --locked python -m llm_ops_lab.olist load \
  --intake work/olist-intake-recovery.nosync/intake.json \
  --output runs/olist/load.json
uv run --locked python -m llm_ops_lab.olist diagnose \
  --output runs/olist/diagnosis.json
```

처음부터 정상 원본 경로를 확보한 환경은 `--intake`를 생략한다. 원본과 암호는 Git에 없으므로
새 컴퓨터에서는 별도 준비가 필요하다. CLI 연결 실패는 암호를 노출하지 않는 error_code로 표시한다.
세부 상태는 `docker compose -f compose.olist.yaml ps`와 소유한 postgres 로그로 확인한다.

## 실패·재처리·게시

원본 검증→Bronze 고정→COPY→SQL 마트→건수/키 대사→포인터 교체를 수행한다.
원본 Decimal 합과 PostgreSQL 저장 합도 대사하되 이를 검증된 통화의 매출로 게시하지 않는다.
COPY 이후 ANALYZE로 새 version_id의 통계를 갱신한다. 기존 통계만 사용하면 새 버전의
실제 대량 행을 한 행으로 추정해 반복 조인이 폭증할 수 있다. 실제 이 현상으로 timeout을
재현했고 이전 정상 게시를 유지한 뒤, 통계 갱신으로 해결했다.
데이터, 게시 포인터, 성공 receipt는 같은 transaction에 commit한다.
실패 receipt는 독립 journal connection에 기록되어 data rollback에 함께 사라지지 않는다.
연결 자체가 끊겨 실패 기록을 못 남기면 다음 실행이 publisher lock 획득 후 이전 running을
abandoned로 표시한다. 원인을 모르는 장애를 자동으로 정상 처리했다고 표시하지 않는다.

다중 계약 판매자는 v1 계약에서 모호성 오류로 거절하며 원본 행을 보존한다.
재확보 없이 Bronze로 재실행하려면 load 결과의 `replay_intake` 경로를 `--intake`로 사용한다.

동시에 두 publisher를 실행하면 하나는 `publisher_busy`로 거절한다.
같은 원본·계약·변환은 같은 version_id로 재사용한다. 새 코드/계약은 새로운 버전이다.
조회는 조사 시작에 version_id를 고정한다. 검증 실패 시 기존 게시 버전은 그대로다.
고정 원본에서 전체 재구축하는 batch다. CDC/실시간 원천/대용량 증분 처리 실적이 아니다.

## 근거 기반 진단

모델을 호출하지 않는 결정론적 기준선이다. 읽기 전용 조회로 품질 보고, 채널/판매자 연결,
최근 실행 이력, 지표 정의를 수집한다. 각 근거 ID는 버전·도구·결과의 해시이고 진단은 이 ID를 참조한다.
조회시간·반환행·출력 크기를 제한하고 초과/누락은 판단 보류한다. 실패한 최근 배치와 현재 정상
게시 데이터의 품질을 혼동하지 않는다. 임의 SQL, 광고 제어, 자동 수정/백필 기능은 없다.

## 재시작과 백업 복원

```bash
docker compose -f compose.olist.yaml restart postgres
docker compose -f compose.olist.yaml ps
uv run --locked python -m llm_ops_lab.olist diagnose
uv run --locked python scripts/olist_backup_verify.py
```

backup verifier는 exported snapshot으로 데이터/실행 원장을 일관되게 pg_dump하고 **새 UUID DB**에
복원한다. 열 개 테이블의 정렬된 행 해시와 건수를 대조한다. 원본 DB/볼륨을 지우거나 덮어쓰지 않는다.
복원 DB와 dump/receipt는 보존한다. 포인터 하나만 확인한 것을 전체 복원 성공으로 주장하지 않는다.
로컬 named volume은 백업 자체가 아니다. 호스트/디스크 상실에 대비한 외부 백업은 후속 범위다.

종료는 다음과 같이 DB만 정지하며 볼륨을 남긴다:

```bash
docker compose -f compose.olist.yaml stop postgres
```

볼륨 삭제, 다른 프로젝트 정지, AWS 생성, 외부 공개는 이 실행 범위에 없다.
