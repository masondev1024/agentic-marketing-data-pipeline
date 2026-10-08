# 합성 마케팅·커머스 보고서
snapshot: 204d305dde9e90264c61838fb316102717c8c7800d913dbe0ca259148954faf0
as_of(UTC): 2026-09-23T12:00:00Z | freshness: fresh
clock(UTC): 2026-09-23T12:00:00+00:00 | fixture_clock_override: True
oldest source watermark: 2026-09-23T12:00:00Z
quality: complete | blocked attempts: 0
Bronze SHA256: 8b32c96ab9c55214b82070dc1f26b7220de75635c72ab2259dbe651ecf905e47
단위: 정수 KRW. 합성 단일 캠페인 귀속; 인과관계·예산 변경 판단 불가.
불완전/오래된 데이터의 관측 합계는 잠정치이며 확정 성과 판단은 unknown.

계정 | KST 날짜 | 광고비 | 주문수 | 매출 | 환불 | 순귀속매출 | ROAS | 품질
---|---|---:|---:|---:|---:|---:|---:|---
alpha | 2026-09-20 | 15000 | 3 | 60000 | 7000 | 53000 | 3.5333 | complete
alpha | 2026-09-21 | 10000 | 2 | 20000 | 0 | 20000 | 2.0000 | complete
beta | 2026-09-20 | 8000 | 1 | 16000 | 0 | 16000 | 2.0000 | complete
beta | 2026-09-21 | 8000 | 1 | 4000 | 0 | 4000 | 0.5000 | complete

캠페인별 관측치 (계정 합계와 별도 grain)
계정 | 캠페인 | KST 날짜 | 광고비 | 순귀속매출 | ROAS | 품질
---|---|---|---:|---:|---:|---
alpha | c1 | 2026-09-20 | 10000 | 43000 | 4.3000 | complete
alpha | c1 | 2026-09-21 | 10000 | 12000 | 1.2000 | complete
alpha | c2 | 2026-09-20 | 5000 | 10000 | 2.0000 | complete
alpha | c2 | 2026-09-21 | 0 | 8000 | unknown | complete
beta | c1 | 2026-09-20 | 8000 | 16000 | 2.0000 | complete
beta | c1 | 2026-09-21 | 8000 | 4000 | 0.5000 | complete

원천 watermark (UTC, 원천 선언이며 독립적인 완전성 증명은 아님)
- alpha/2026-09-20/spend: 2026-09-23T12:00:00Z complete=True
- alpha/2026-09-20/orders: 2026-09-23T12:00:00Z complete=True
- alpha/2026-09-20/refunds: 2026-09-23T12:00:00Z complete=True
- alpha/2026-09-21/spend: 2026-09-23T12:00:00Z complete=True
- alpha/2026-09-21/orders: 2026-09-23T12:00:00Z complete=True
- alpha/2026-09-21/refunds: 2026-09-23T12:00:00Z complete=True
- beta/2026-09-20/spend: 2026-09-23T12:00:00Z complete=True
- beta/2026-09-20/orders: 2026-09-23T12:00:00Z complete=True
- beta/2026-09-20/refunds: 2026-09-23T12:00:00Z complete=True
- beta/2026-09-21/spend: 2026-09-23T12:00:00Z complete=True
- beta/2026-09-21/orders: 2026-09-23T12:00:00Z complete=True
- beta/2026-09-21/refunds: 2026-09-23T12:00:00Z complete=True
