# Nemotron Super HTTP 조사 서비스

`/v1/investigations`를 NVIDIA Nemotron 3 Super에 연결하는 명시적 선택 경로다.
기본 `local` 설정은 기존 로컬 모델을 사용한다. `nvidia-super`는 `intent-v1`만 지원한다.

## 데이터 흐름과 경계

1. 로컬 HTTP 서버가 질문·버전 ID·대기열을 검증한다.
2. supervisor가 원격 호출 2회분을 예약하고 남은 요청 시간을 워커에 전달한다.
3. NVIDIA에는 고정 분류 지침과 **사용자가 입력한 질문**만 전송한다.
4. 모델의 JSON 계획을 검증한 뒤, 호스트가 허용된 읽기 전용 SQL을 실행한다.
5. 게시 버전·품질 게이트·근거를 확인하고 호스트가 finding을 만든다.

DB 행, 조회 결과, 연결 문자열은 모델 입력에 넣지 않는다. 질문 자체는 외부로 전송되므로
공개 Olist 데이터에 관한 질문을 사용하고 고객사 비공개 정보나 개인정보를 입력하지 않는다.
모델이 임의 SQL을 실행하거나 DB를 변경할 수 없다. 외부 로그가 없는 원인 확정 요청과
실제 데이터 삭제 요청은 서로 다른 의미의 보류 계획으로 처리한다.

고정 모델은 `nvidia/nemotron-3-super-120b-a12b`, 엔드포인트는
`https://integrate.api.nvidia.com/v1/chat/completions`다. 리디렉션·환경 프록시·로컬 모델
자동 폴백은 사용하지 않는다. temperature=0, seed=42, 출력 최대 256토큰,
`enable_thinking=false`를 사용한다. 호스팅 가중치 revision은 고정할 수 없어 응답의
`revision_pinned`는 false다.

## 실행

게시된 Olist 데이터가 있는 Docker PostgreSQL이 필요하다. 로컬 MLX 모델은 적재하지 않으며
`--extra local`도 필요 없다. 이 모드의 `/v1/answers`는 503 `answers_disabled_for_provider`다.

키는 기존 `work/olist/secrets/nvidia-api-key.txt`를 그대로 읽는다. 소유자가 본인이고 다른
사용자의 읽기·쓰기 권한이 없는 일반 파일이어야 한다(예: 권한 600). `NVIDIA_API_KEY` 환경
변수가 있으면 우선 사용한다. 키를 CLI 인자, 요청 JSON, Git에 넣지 않는다.

```bash
uv run --locked llm-lab-serve \
  --olist-root "$PWD" --olist-engine intent-v1 --olist-provider nvidia-super \
  --remote-call-budget 32 --request-timeout 60 --port 8011
```

다른 터미널에서:

```bash
curl -sS http://127.0.0.1:8011/v1/investigations \
  -H 'Content-Type: application/json' \
  -d '{"question":"커머스 표본에 연결되지 않은 판매자 계약은 몇 개인가요?"}'

curl -sS http://127.0.0.1:8011/metrics | python3 -m json.tool
```

`/health/ready`는 워커와 로컬 설정 준비 상태다. NVIDIA 계정 권한·서비스 가용성 또는
PostgreSQL 데이터 준비를 보장하는 외부 헬스 체크가 아니다. 실제 조사를 실행해야 확인된다.

## 호출 상한과 실패 처리

- 대기열과 실행 슬롯을 합친 기본 수용량은 3이다. 초과는 기존대로 HTTP 429다.
- 분류 JSON 형식 수정은 최대 1회, 요청당 모델 호출은 최대 2회다.
- **dispatch 직전 2회분을 예약**한다. 실제 1회로 끝나도 예약량을 환급하지 않는다.
  `remote_budget.reserved`는 실제 호출·청구 토큰 수가 아니다.
- 기본 예약 상한은 32(최대 16개 요청), 설정 범위는 2~128이다. 소진 뒤에는
  503 `remote_call_budget_exhausted`로 외부 호출 전에 거절한다.
- 이 상한은 **API 프로세스 한 번의 실행 기간**에 적용되며 워커 교체에도 유지된다.
  API 서버 자체를 재시작하면 초기화된다. 계정 전체의 영구 비용 한도나 일일 청구 한도가 아니다.
- 네트워크·429·5xx에 대한 자동 재시도는 없다. 원격 실행 여부가 불명확한 요청을 재전송하면
  중복 비용이 생길 수 있으므로 클라이언트도 무한 재시도를 하지 않는다.
- 대기 시간이 전체 deadline에 포함된다. 강제 deadline은 워커 종료·회수 후 복구한다.
  원격 서버의 추론 중단이나 비용 취소까지 보장하지 않는다.
- 호출자의 작업 취소로 실행 슬롯을 먼저 반환하지 않는다. 취소된 대기 작업은 실행하지 않으며,
  이미 시작한 작업은 완료 또는 deadline까지 슬롯과 예약을 유지한다. 브라우저 연결 종료만으로
  NVIDIA의 원격 추론이 취소된다고 주장하지 않는다.

| 상황 | HTTP | 오류 코드 |
|---|---:|---|
| 근거 부족·지원 불가 요청 | 200 | 정상 보류 응답의 `request_plan`·`stop_reason` |
| NVIDIA 타임아웃 | 504 | `nvidia_upstream_timeout` |
| 전체 요청 deadline | 504 | `inference_deadline_exceeded` |
| NVIDIA 429 | 503 | `nvidia_rate_limited` |
| NVIDIA 인증 거절 | 503 | `nvidia_authentication_failed` |
| NVIDIA 5xx·통신·응답 계약 오류 | 502 | `nvidia_upstream_unavailable` 등 제한된 코드 |
| 서버 호출 예약 소진 | 503 | `remote_call_budget_exhausted` |

오류 응답에 공급자의 응답 본문·키·SQL·DSN은 넣지 않는다. 완료된 워커 실행 기록에는
`provider`, `provider_error`, `remote_call_attempts`, 토큰 사용량 및 실행 상태를 보존한다.
공급자 응답을 받지 못한 호출의 토큰 사용량은 알 수 없으며 0으로 청구되었다는 뜻이 아니다.
요청 ID로 개별 JSON과 `access.jsonl`을 연결한다. `/metrics`에 원격 예약량,
`upstream_errors`, `remote_budget_rejected`가 포함된다.

## 검증 범위와 다음 단계

단위 테스트는 실패 응답, 비밀 보호, 형식 수정, 예산·워커 복구를 검증한다. 실제 NVIDIA
호출은 연결·현재 모델 행동의 별도 증거다. 이전 품질 평가를 이번 HTTP 실측으로 대체해
주장하지 않는다. AWS 배포·외부 사용자 인증·다중 서버 공용 비용 한도·영구 재시작 예산은
다음 배포 설계 범위다. 현재 서버는 loopback 전용이다.

실제 HTTP 호출 2건과 예약 소진 차단 기록: [연결 검증 증거](../evidence/olist-nvidia-http-20261010/report.json).
두 문항은 기존에 사용한 거절 사례이며 독립 품질 평가가 아니다. 10월 10일에는 Docker
미기동·메모리 경고로 PostgreSQL 실측을 보류했고, 당시 자동 검사는 672개 통과·15개 건너뜀이었다.

2026-10-12에는 기존 `llm-ops-olist_postgres_data` 볼륨과 종료된 컨테이너가 남아 있음을
확인하고, 재적재 없이 컨테이너를 재시작했다. 실제 Super HTTP 요청으로 미연결 계약
**462/842**, origin 누락 리드 **60/8000**을 조회해 DB 집계와 일치함을 확인했다.
두 요청의 근거 참조·요청 ID·로그 연결과 워커 재사용을 검증했고, 게시 버전·품질·채널/판매자
집계·최근 실행 이력의 조회 전후 해시가 같았다. 전체 원본 행 해시 검사나 독립 품질 평가로
확대 해석하지 않는다. [PostgreSQL 실측 기록](../evidence/olist-nvidia-http-postgres-20261012/report.json).
검증용 HTTP 서버와 워커는 종료했고 8012 포트 해제를 확인했다.

최종 PostgreSQL 통합 포함 검사: **687개 통과·건너뜀 0개**, Ruff 통과. 임시 테스트 DB는
모두 정리됐고 전체 검사 후에도 기존 게시 근거가 동일했다. 검증 후 PostgreSQL 컨테이너를
정지하고 named volume은 보존했다. 컨테이너 정지는 DB 데이터 삭제가 아니다.
