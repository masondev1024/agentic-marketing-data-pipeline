# 로컬 운영·복구 가이드

조력자가 검증한 로컬 절차다. [실행 예제](README.md)의 새 출력 디렉터리에서 시작한다.
원본 `work/`, `runs/`, `.venv`와 사용자 localhost:8000은 정리하거나 재시작하지 않는다.
로그·checkpoint·원시 실행은 Git 제외 영역에 두고 검토한 소규모 합성 증거만 공개한다.

| 증상 | 확인할 증거 | 안전한 다음 행동 |
|---|---|---|
| schema/key/payload 충돌 | CLI stderr JSON, quarantine input/failure, 이전 CURRENT와 artifact hash | 원천 revision·키 오류를 고친 전체 창을 재입력. 충돌을 임의로 최신 선택하지 않음 |
| ROAS unknown | complete, as_of, oldest watermark, 0 지출 여부 | 누락 원천을 복구하고 새 snapshot 재진단. 예산 판단은 보류 |
| 지연 주문·환불 | event_time/ingested_at/as_of 및 원주문 날짜 | 원주문을 포함하는 고정 창 전체 backfill. 다른 창은 별도 root |
| writer_busy/resume_busy | 해당 로컬 작업 프로세스와 flock 소유 여부 | 소유한 실행 종료 후 재시도. 잠금 파일 삭제를 복구로 삼지 않음 |
| CURRENT/artifact 손상 | manifest SHA와 파일 SHA | 소비 중단, 검증된 입력·백업으로 별도 root 재구축 후 명시적 경로 전환 |
| graph stale_snapshot / expired | thread binding, 현재 게시, 승인 시각 | 기존 증거로 승인하지 않음. 새 snapshot·새 thread로 명시적 재진단 |
| graph pending crash | 실행 원장과 terminal interrupted_action | 결과 unknown, 남은 예산 소진. 같은 thread 예산 초기화 금지 |
| completed/review 뒤 crash | 완료 원장 및 proposal/decision | 동일 scope·decision으로 새 프로세스 resume. 결과·기록 재사용 |
| 모델 evidence_incomplete / timeout | 실제 generations, observations, usage, error | 오류를 unknown으로 공개. 기준선 답변을 모델 성공으로 대체하지 않음 |

backfill 뒤 이전/새 manifest·Gold 수치·freshness를 비교하고 같은 입력 재실행의 snapshot
동일성을 확인한다. 오래된 유효본이 남는 것은 최신 수집 성공이 아니다. 실패 횟수와 마지막
성공 시각을 함께 읽어야 한다. 상세 실행은 [데이터 복구](data-product.md)와
[새 프로세스 재개](graph-recovery.md)에 있다.

로컬 관측 항목은 품질 상태, blocked attempts, snapshot/fixture revision, watermark,
도구 calls/actions/token units, 실제 모델 tokens·latency·errors, graph terminal 상태다.
대시보드·상시 알림은 미구현이다. 향후 운영에서는 게시 실패·watermark 지연·integrity 오류·
반복 unknown을 알림으로 연결하되 제품별 실제 수집 SLA를 먼저 정한다. 24시간 freshness는
현재의 조력자 기본값이며 운영 SLA가 아니다.

SQLite를 백업할 때 실행을 멈추고 일관된 DB backup과 참조 snapshot·코드/계약 버전을
함께 보존해야 한다. 실행 중 DB 파일만 복사하는 방법은 검증하지 않았다. 복구 시 절대 root
binding이 달라지면 거절하므로 원래 경로를 복구하거나 새 DB·thread에서 재진단한다.
프로세스 crash 테스트는 장비/디스크 동시 고장, NFS, 멀티호스트 장애 복구 보증이 아니다.
외부 부작용이 없어 exactly-once 외부 실행을 주장하지 않는다.

실험 재실행은 [모델 실험](model-experiment.md)의 오프라인 절차를 따른다. 16GB Mac에서
한 번에 추론 워커 하나만 실행하고 기존 서비스가 추론 중이면 새 실험을 시작하지 않는다.
학습자의 후속 실습은 권장하되 이번 프로젝트 합격의 추가 필수 조건으로 만들지 않는다.
