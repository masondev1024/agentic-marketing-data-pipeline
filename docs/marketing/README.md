# Marketing & Commerce Data Reliability Lab

“어제 ROAS가 하락했다. 실제 관측 변화인가, 데이터 누락·오류인가?”를 재현하는
로컬 합성 데이터 프로젝트다. 원인은 unknown으로 남기고 광고 예산을 변경하지 않는다.
조력자가 구현·검증했으며 학습자의 마케팅 단계 직접 실행·독립 설계 역량은 아직 입증하지 않았다.

권위 입력은 저장소의 `AGENTS.md`와 `work/marketing-aws-implementation-brief.md`다.
브리프 원문·기존 입력을 보존했다. 아래 이름은 브리프의 일곱 단계에 대한 한국어 요약이며
새 요구사항이 아니다. 짧은 요구사항 연결은 [검토 기록](../../evidence/marketing-ac6-docs.json)에 둔다.

| 순서 / 브리프 단계 | 산출물·실행 증거 | 결과와 남은 한계 |
|---|---|---|
| 1. Domain/data contracts | [계약·독립 산술](data-product.md), [기계 계약](../../data/marketing/v1/contract.json) | 두 계정, 정수 KRW, UTC/KST, 고정 합성 귀속. 실제 광고 기여도 모델 아님 |
| 2. Python + SQL + DuckDB/Parquet pipeline | [마케터 보고서](acceptance-report.md), [게시 manifest·수치](../../evidence/marketing-ac2-data-product.json) | 중복·정정·지연 환불·원자적 게시 검증. 로컬 single writer·31일 전체 창 |
| 3. Harness | [진단 계약](diagnostic-interface.md), [AC3 증거](../../evidence/marketing-ac3-verification.json) | 불변 scope와 읽기 전용 네 도구. 원천 완료 선언은 독립 수집 감사가 아님 |
| 4. Deterministic baseline + model-selected loop | [실험 결과](model-experiment.md), [실제 추론 증거](../../evidence/marketing-ac4-experiment.json) | 모델이 도구 선택, 동일 snapshot 비교 완료. 정답·증거 완전성 0/3, 실험용 유지 |
| 5. Persisted conditional graph | [그래프·복구 절차](graph-recovery.md), [18개 복구 사례](../../evidence/marketing-ac5-recovery.json) | 새 프로세스 SQLite 재개·제안 승인/거절 검증. 결정론적 그래프이며 외부 exactly-once 보장 없음 |
| 6. Evaluation and operations evidence | [종합 판정](results.md), [동결 기대값](../../data/marketing/v1/acceptance-expected.json) | 필수 안전·복구·CLI/API 회귀와 실제 모델 실패 측정. 작은 합성셋이며 운영 일반화 불가 |
| 7. Korean docs + optional AWS design | [ADR](architecture.md), [운영](operations.md), [직무·포트폴리오](portfolio.md), [AWS 설계](aws-migration.md) | 문서·명령·증거 대조. AWS는 선택 설계만 작성했고 리소스를 만들지 않음 |

판정은 [results.md](results.md)를 기준으로 읽는다. 필수 로컬 검증 통과와 에이전트 승격은
별개다. 수정 코드의 실제 추론 재검증 증거와 현재 회귀를 함께 검토한다.
모델의 낮은 품질을 숨기지 않으며 안전·복구 검사 실패를 실험용 명칭으로 면제하지 않는다.

## 원본을 변경하지 않는 재현

Python 3.12와 uv가 필요하다. 아래 관찰자는 원본을 읽어 외부 임시 복사본과 독립 uv 환경을
만들고 그곳에서만 검사 상태를 쓴다. AC6 검토 기록의 문서·증거·모델 소스 해시를 전후
대조한다. 동시 작업 중인 다른 AC의 비검토 파일은 감시에서 제외하지만 전체 테스트는 실행한다.
모델 추론과 사용자 localhost:8000은 사용하지 않는다.

```sh
python3 scripts/verify_marketing_ac6.py -- uv run --locked python scripts/check_marketing_docs.py
python3 scripts/verify_marketing_ac6.py -- uv run --locked pytest -q tests/test_marketing_docs.py
python3 scripts/verify_marketing_ac6.py -- uv run --locked pytest
python3 scripts/verify_marketing_ac6.py -- uv run --locked pytest -q
python3 scripts/verify_marketing_ac6.py -- uv run --locked ruff check .
```

문서 검사 JSON은 일곱 산출물의 경로와 증거 해시, 실제 모델 점수, 승격 판정을 대조한다.
문서 테스트는 아래 명령 블록을 그대로 실행해 보고서와 새 프로세스 제안 검토까지 확인한다.
실제 MLX 추론을 다시 수행한 것으로 세지 않는다. 이전 실제 실행의 revision·원시 출력은
[실험 문서](model-experiment.md)에 있으며 재추론에는 기존 오프라인 모델 캐시가 필요하다.

## 실행 예제

저장소 루트에서 다음 전체 블록을 bash로 실행한다. 출력은 새 외부 임시 디렉터리에 남기며
자동 삭제하지 않는다. 관찰자 수용 검증에서는 외부 복사본 안에서 실행된다.
합성 시계는 과거 fixture 재현용으로, 현재 신선도·실서비스 운영 증거가 아니다.

<!-- marketing-walkthrough:start -->
```bash
set -eu
LAB_RUN_DIR="$(mktemp -d "${TMPDIR:-/tmp}/marketing-walkthrough.XXXXXX")"
export LAB_RUN_DIR
uv run --locked python -m llm_ops_lab.marketing build \
  --input data/marketing/v1/acceptance.json --output-dir "$LAB_RUN_DIR/product" > "$LAB_RUN_DIR/build.json"
uv run --locked python -m llm_ops_lab.marketing report \
  --output-dir "$LAB_RUN_DIR/product" --now 2026-09-23T12:00:00Z > "$LAB_RUN_DIR/report.md"
SNAPSHOT_ID="$(uv run --locked python -c 'import json,os; from pathlib import Path; print(json.loads((Path(os.environ["LAB_RUN_DIR"])/"product/CURRENT.json").read_text())["snapshot_id"])')"
uv run --locked python -m llm_ops_lab.marketing.graph_cli start \
  --root "$LAB_RUN_DIR/product" --database "$LAB_RUN_DIR/checkpoints.sqlite" \
  --thread walkthrough --snapshot "$SNAPSHOT_ID" --account alpha \
  --start 2026-09-20 --end 2026-09-21 --evaluation-now 2026-09-23T12:00:00Z > "$LAB_RUN_DIR/start.json"
uv run --locked python -m llm_ops_lab.marketing.graph_cli resume \
  --root "$LAB_RUN_DIR/product" --database "$LAB_RUN_DIR/checkpoints.sqlite" \
  --thread walkthrough --snapshot "$SNAPSHOT_ID" --account alpha \
  --start 2026-09-20 --end 2026-09-21 --evaluation-now 2026-09-23T12:00:00Z \
  --decision reject > "$LAB_RUN_DIR/review.json"
printf '%s\n' "$LAB_RUN_DIR"
```
<!-- marketing-walkthrough:end -->

`report.md`에서 alpha 9/20 순귀속매출 53,000원·광고비 15,000원, 9/21 20,000원·10,000원을
확인한다. `start.json`은 review 대기, `review.json`은 제안 거절 기록이다. reject는
데이터 삭제나 광고 변경이 아니다. 기존 CLI/API는 [루트 README](../../README.md)를 따른다.
