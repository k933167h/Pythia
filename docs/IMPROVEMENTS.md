# PYTHIA v0.3 개선 보고서

예측 정확도·검증, 보안·거버넌스, 엔터프라이즈 운영, 한국 공공·기업 적용의 네 축으로 개선했다. 근거는 2024~2026년 최신 논문·표준·컨설팅 자료이며, 이 문서는 무엇을 왜 바꿨는지와 남은 개선점을 정리한다.

## 1. 요약

| 구분 | v0.2 (원본) | v0.3 (개선) |
|---|---|---|
| 스웜 집계 | 산술 가중평균 | 로그오즈 가중평균(극단화 계수 설정 가능), 투표값 5~95% 클램프 |
| 시장 확률 | 프롬프트에 참고로만 제공 | 같은 질문의 Polymarket·Kalshi·Manifold 가격과 로그오즈 산술 혼합 |
| 보정 | 없음 | 호라이즌별 Platt 재보정(감쇠 뉴턴 + 사전분포 + 범위 제한) |
| 채점 | 예측한 모델이 직접 판정 | ① 측정 데이터로 결정론적 판정 → ② 다른 모델의 독립 판정자 → ③ 표본 인간 검토 |
| 판정 기준 | 판정 시점에 해석 | 예측 시점에 `resolution_criteria` 고정 |
| 지표 | Brier, 적중률 | Brier, 로그손실, ECE, 기후값 대비·시장 대비 스킬 점수, YES 편향 |
| 인증 | 없음 (0.0.0.0, CORS `*`) | 기본 루프백 전용, 읽기·관리 토큰 범위, CORS 허용목록 |
| 프롬프트 인젝션 | 무방비 | 정규화·무력화·`<world_data>` 격리·시스템 규칙 |
| 웹훅 | 임의 URL로 평문 POST | SSRF 차단 + IP 고정, Standard Webhooks HMAC 서명, 재시도 |
| 감사 | 없음 | 해시 체인 감사로그, 세계 스냅샷 해시 체인 |
| 투명성 | 없음 | 모든 출력에 AI 생성 표시 + 출처 정보(provenance) |
| 운영 | 로그만 | Prometheus `/metrics`, `/healthz`·`/readyz`, LLM 일일 예산 |
| 테스트 | 0개 | 78개 + CI(Python 3.11~3.13) |
| 한국 | 없음 | 국문 출력(`ORACLE_LANG=ko`), 기상청 지진·에어코리아 피드(실험), 한반도 가중치 |

## 2. 조사 근거

### 2.1 LLM 예측 연구

- **Halawi 외, "Approaching Human-Level Forecasting with Language Models"(NeurIPS 2024).** 시스템 Brier 0.179, 군중 0.149였고, 둘을 평균하면 0.146으로 어느 쪽보다 좋았다. 검색과 다중 샘플 집계가 보정의 핵심이었다. → **시장 혼합, 근거 수 게이트.** https://arxiv.org/abs/2402.18563
- **ForecastBench(FRI, ICLR 2025)와 2026년 1월 업데이트.** 최상위 LLM의 난이도 보정 Brier는 약 0.10이고 슈퍼예측가와의 차이는 0.017이다. 시장 가격과 비교해 조정하는 방식만으로 약 0.01이 개선됐다. → **스킬 점수 보고, 시장 앵커.** https://forecastingresearch.substack.com/p/llms-are-closing-the-gap-on-human
- **Metaculus FutureEval(2025~2026).** 앙상블과 중앙값 집계가 효과적이었고 10개를 넘으면 오히려 나빠졌다. Platt 스케일링이 사후 보정 5종 중 최선이었다(Brier −0.016). → **Platt 재보정, median 옵션.** https://www.lesswrong.com/posts/wZBbDqzfBjYG58CxK/futureeval-spring-results-pros-beat-bots-but-the-gap-is
- **Schoenegger 외, "Wisdom of the Silicon Crowd"(Science Advances 2024).** LLM은 YES 쪽으로 치우친다(평균 예측 57% vs 실제 45%). 인간 중앙값을 보여주고 모델이 갱신하게 하는 것보다 단순 평균이 더 나았다. → **YES 편향 지표, 프롬프트가 아닌 산술 혼합.** https://pmc.ncbi.nlm.nih.gov/articles/PMC11800985/
- **Satopää 외(IJF 2014), Baron 외(Decision Analysis 2014).** 로그오즈 집계와 극단화를 다룬다. 다만 정보가 독립적이지 않으면 극단화의 이득이 없다. → **기본 극단화 계수 1.0**(페르소나는 같은 모델·같은 맥락을 공유). https://arxiv.org/abs/1406.2148
- **Paleka 외, "Pitfalls in Evaluating LM Forecasters"(2025).** 지식 컷오프 누출과 시장 가격 편승 문제를 지적하고 Brier·로그점수·보정을 함께 보고하라고 권한다. https://arxiv.org/abs/2506.00723
- **"Simulated Ignorance Fails"(2026).** "날짜 X 이후는 모른다고 가정하라"는 지시로는 누출 격차의 48%만 줄어든다. → **백테스트는 모델 출시 이후 질문만 사용**(로드맵). https://arxiv.org/abs/2601.13717
- **Panickssery 외, "LLM Evaluators Recognize and Favor Their Own Generations"(NeurIPS 2024).** LLM 평가자는 자기 생성물을 선호한다. → **결정론적 판정 우선, 다른 모델의 판정자, 확률 비공개, 표본 인간 검토.** https://arxiv.org/abs/2404.13076

### 2.2 보안

- **OWASP Top 10 for LLM Applications(2025, 2026 개정판).** 프롬프트 인젝션, 과도한 권한, 무제한 소비, 공급망 위험을 다룬다. https://genai.owasp.org/
  - 2026 항목명은 CSA 정리본으로만 확인했다.
- **OWASP Top 10 for Agentic Applications 2026(2025-12).** ASI01 목표 탈취, ASI03 권한 남용, ASI06 메모리·컨텍스트 오염, ASI08 연쇄 실패를 다룬다. https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026
- **MCP Security Best Practices(사양 2025-11-25).** 로컬 HTTP 서버는 토큰 또는 루프백을 요구하고 DNS 리바인딩을 방어해야 한다. SSRF 대응으로 사설 대역 차단, 리다이렉트마다 재검증, DNS 고정을 권한다. https://modelcontextprotocol.io/specification/2025-11-25/basic/security_best_practices
- **Standard Webhooks 사양.** `webhook-id`·`webhook-timestamp`·`webhook-signature` 헤더와 HMAC-SHA256 서명을 정의한다. 이번 구현은 사양 저장소의 참조 벡터로 검증했다. https://github.com/standard-webhooks/standard-webhooks

### 2.3 거버넌스·규제

- **NIST AI RMF 1.0과 생성형 AI 프로파일(NIST AI 600-1).** MEASURE(타당성·보정)와 MANAGE(모니터링)를 설계의 뼈대로 삼았다. https://www.nist.gov/itl/ai-risk-management-framework
- **ISO/IEC 42001:2023(AI 경영시스템), ISO/IEC 42005:2025(영향평가).** https://www.iso.org/standard/42005
- **인공지능기본법(2026-01-22 시행).**
  - 제31조 투명성: 생성형 AI임을 사전에 고지하고 결과물을 표시해야 한다.
  - 제34조: 고영향 AI 사업자의 책무를 정한다.
  - 과태료는 계도기간 이후 부과될 예정이다.
- **EU AI Act.** 제50조 투명성 의무가 2026-08-02부터 적용됐다(Digital Omnibus 반영).
- **국정원 「AI 보안 가이드북」(2025-12).** 위협 15종, 대응 30종, 체크리스트 57항목. https://www.cio.com/article/4103809/
- **행안부 「공공부문 AI 윤리기준」(2026-10).** 6대 가치를 제시하고 최종 책임과 통제는 사람에게 있다고 규정한다. 자율점검 항목은 68개다.

### 2.4 컨설팅·산업

- **Gartner(2025-06).** 2027년 말까지 에이전틱 AI 프로젝트의 40% 이상이 비용, 가치 불명확, 위험 통제 부족으로 중단될 것으로 전망했다. → **예측 1건당 비용과 시장 대비 가치를 지표화.**
- **Deloitte State of AI in the Enterprise 2026.** 에이전트 사용을 기대하는 기업은 74%인데 성숙한 거버넌스를 갖춘 곳은 21%에 그친다.
- **McKinsey "Seizing the Agentic AI Advantage"(2025).** 자율성 임계와 인간 승인 지점을 명시하는 "Governance 2.0"과 감사 로깅을 권고한다.
- **BCG Henderson Institute.** 장기 호라이즌은 점 확률보다 시나리오 범위로 제시하라고 권한다(로드맵).

## 3. 장점 극대화와 단점 보완

### 3.1 극대화한 장점

- **실시간 시장 확률이라는 고유 자산.** 지금까지는 프롬프트 장식에 그쳤다. 이제는 같은 질문의 시장 가격과 산술 혼합하고, 시장 대비 스킬 점수로 가치를 직접 측정한다.
- **스웜 심의.** 로그오즈 집계와 성과 기반 가중치로 다양성의 이득을 살렸다. 한 목소리가 극단값을 내도 전체를 끌고 가지 못하게 했다.
- **자기 채점 장부.** 예측 시점에 판정 기준을 고정하고 판정을 독립시켰다. 결정론적 판정, 품질 지표, 재보정을 더해 측정 결과가 예측을 개선하는 학습 고리를 완성했다.
- **에이전트 API.** 출처 정보, 신뢰 플래그(`low_evidence`, `market_anchor`), AI 표시를 붙였다. 에이전트가 안전하게 소비할 수 있는 "지구의 눈"이 됐다.

### 3.2 보완한 단점

- **무인증 노출.** 루프백 기본, 토큰 범위, CORS 허용목록, 호스트 헤더 검사, 프록시의 서버측 토큰 주입으로 막았다.
- **SSRF와 위조.** 모든 해석 IP를 검사하고, 연결을 검사한 IP에 고정하고, HMAC 서명을 붙였다.
- **순환 채점.** 측정 데이터와 독립 판정자를 쓰고, 판정자에게 확률을 감추고, 인간 검토 표본을 둔다.
- **테스트 부재.** 테스트 78개와 CI를 갖췄다.
- **커밋된 로그.** `runs/engine.log`를 제거하고 `.gitignore`를 강화했다.

### 3.3 함께 발견해 고친 기존 버그

- **대시보드에서 삭제가 동작하지 않았다.** 프록시에 `DELETE` 처리기가 없어 워치리스트·알림 규칙 삭제가 405로 실패했다.
- **피드 하나가 통째로 버려졌다.** 선택 필드가 `None`인 항목 하나(예: url이 없는 Kalshi 시장)가 검증 오류를 내면 해당 피드 전체가 조용히 폐기됐다. Kalshi 시장 확률도 원시 데이터에 남지 않았다.

## 4. 구현 내역

| 모듈 | 역할 |
|---|---|
| `engine/calibration.py` | 로그오즈 집계·극단화, 시장 혼합, 질문 매칭(Jaccard, 공유 단어 3개 이상, 부정 극성 일치), Platt 적합, Brier·로그손실·ECE·스킬 점수 |
| `engine/quality.py` | 예측 후처리 파이프라인(시장 혼합 → Platt → 근거 게이트 → 출처 정보 스탬프), AI 고지문(국/영) |
| `engine/resolver.py` | 결정론적 판정. 지진은 발생 시각 기준·반경 600km·피드 커버리지 50% 이상, 시장은 창 안에서 마감되고 99%/1%로 확정된 경우. 부정문은 판정자에게 넘김 |
| `engine/sanitize.py` | 비신뢰 텍스트 정규화와 인젝션 문구 무력화(국/영), `<world_data>` 격리 |
| `engine/security.py` | ASGI 인증 미들웨어(SSE 호환), 범위, 사용자별 속도 제한, 256KB 본문 제한(청크 전송 포함), 보안 헤더 |
| `engine/netguard.py` | 웹훅 SSRF 가드와 검증 IP 고정(Host·SNI 유지) |
| `engine/webhooks.py` | Standard Webhooks 서명, 비밀키 1회 노출·파일 권한 0600, 백오프 재시도, 410이면 비활성화, AI 고지 동봉 |
| `engine/audit.py` | 해시 체인 감사로그(모델 교체, 웹훅, 알림, 루프, 인간 판정, 접근 거부, 예산 소진, 예측 패스) |
| `engine/observability.py` | Prometheus 지표, LLM 24시간 예산 |
| `engine/governance.py` | 시스템 카드: 용도·금지 용도·한계·성능·통제-표준 매핑 |
| `engine/korea_intake.py` | 기상청 지진정보·에어코리아(키 필요, 실험적), 한반도 범위 판정 |
| `engine/oracle.py` | 판정 기준 요청, 측정 가능한 진술 유도, 독립 판정자(다른 모델·온도 0·키 유출 방지), 국문 출력 |
| `integrations/osiris/routes/engine-proxy-route.ts` | 서버측 토큰 주입, 관리 경로 차단, 읽기 전용 모드, 경로 정규화, `DELETE` 추가, 사용자 전달 |
| `integrations/osiris/PythiaPanel.tsx` | 시장 앵커·보정·근거 부족·판정 기준 배지, AI 생성 고지 |

### 새 엔드포인트

`/healthz` · `/readyz` · `/metrics` · `/governance/system-card` · `/governance/audit` · `/governance/audit/verify` · `/governance/review-queue` · `POST /governance/review/{id}` · `/governance/calibration`

## 5. 검증

- **pytest 78개 통과.** 수학, 보안 단위, 장부·판정, HTTP 표면, 가짜 LLM을 쓴 종단 간 패스를 다룬다. Standard Webhooks 참조 서명 벡터와 일치하는 것도 확인했다.
- **실제 기동 스모크 테스트.** uvicorn과 가짜 OpenAI 호환 LLM, 가짜 Osiris로 확인했다.
  - 토큰 없이 요청하면 401, 읽기 토큰으로 상태를 바꾸려 하면 403, 읽기 토큰으로 `/chat`은 200
  - 국문 응답과 국문 AI 고지
  - 메타데이터 IP(169.254.169.254) 웹훅 거부
  - 감사 체인 검증 통과
  - 인젝션 6건 탐지가 `/metrics`에 집계
  - 시장 혼합 확인(평의회 55% + Kalshi 85% → 72.5%)
- **프록시 TypeScript.** `tsc --strict` 통과.
- **독립 코드 리뷰.** 별도 에이전트가 결함 13건을 찾았고(High 3, Medium 8, Low 2) 모두 수정한 뒤 회귀 테스트를 추가했다.
  - Platt 적합 발산
  - 지진 판정이 발생 시각이 아닌 수집 시각을 사용
  - 읽기 토큰으로 `/chat`을 쓸 수 없음
  - 부정문 오매칭
  - "3%" 같은 임계값을 시장 확률로 오인
  - DNS 리바인딩
  - 판정자 키 유출

## 6. 운영 가이드(요약)

```bash
# 개인 PC(기본값): 아무 설정 없이 루프백 전용
./run-all.sh

# 팀 공유: 토큰 + 독립 판정자 + 국문
export PYTHIA_ADMIN_TOKEN=$(openssl rand -hex 32) PYTHIA_READ_TOKEN=$(openssl rand -hex 32)
export JUDGE_MODEL=gemma3:12b ORACLE_LANG=ko LLM_DAILY_BUDGET=2000
# Osiris(.env.local): PYTHIA_ENGINE_TOKEN=<관리 토큰>, 공용 화면이면 PYTHIA_PROXY_READ_ONLY=true
# MCP: PYTHIA_ENGINE_TOKEN=<읽기 토큰>
```

전체 설정은 `.env.example`, 위협 모델은 `SECURITY.md`를 참조한다.

## 7. 남은 개선점(로드맵)

### 단기 (1~2주)

1. **모델 다양성.** 평의회에 2~3개 모델 계열을 섞는다. 같은 모델에 페르소나만 바꾸면 오류가 상관된다. FutureEval 2025년 가을 라운드 상위 봇의 86%가 여러 예측을 집계했다.
2. **판정 검토 화면.** 인간 검토 대기열(`/governance/review-queue`) UI를 만들고, 스코어카드에 시장 대비 스킬과 보정 곡선을 표시한다.
3. **웹훅 이그레스 프록시.** Smokescreen 같은 이그레스 프록시를 거치게 해서 방어를 이중화한다.
4. **보관 정책.** 장부·감사로그의 순환과 보존 기간을 정한다(ISO 42001 7.5).

### 중기 (1~2개월)

5. **질문별 검색 에이전트.** 예측마다 원문 기사를 검색해 근거를 보강한다(Halawi 방식). 지금은 헤드라인 수준이다.
6. **누출 통제 백테스트 하네스.** 모델 출시 이후에 생성되고 해결된 질문만 사용한다. ForecastBench에 제출해 외부에서 검증받는다.
7. **장기 호라이즌 시나리오화.** 월·연 호라이즌은 점 확률 대신 시나리오 범위와 선행 지표를 제시한다(BCG).
8. **신원 기반 접근.** Osiris에 SSO(OIDC)를 붙여 사용자 신원을 엔진 감사로그까지 전달한다. 공유 토큰 대신 사용자별 권한을 둔다.
9. **한국 피드 확장과 실검증.**
   - 추가 대상: 긴급재난문자(safetydata.go.kr), 한국은행 ECOS, KRX
   - 기존 기상청·에어코리아 엔드포인트는 실제 키로 검증한다. 이번 작업 환경은 외부망이 막혀 있어 픽스처로만 시험했다.

### 장기 (분기)

10. **공급망 통제.** 모델 다이제스트를 고정하고 검증하며, AI BOM을 관리한다(OWASP LLM03·ASI04).
11. **영향평가 템플릿.** ISO/IEC 42005 영향평가와 인공지능기본법 고영향 해당 여부 판단 절차를 문서화한다. 개인 대상 의사결정에 쓰지 않는다는 금지 용도를 계약과 운영에 반영한다.
12. **가치 지표.** 예측 1건당 LLM 비용과 시장 대비 스킬을 경영 KPI로 정의한다. Gartner가 지적한 40% 중단 위험에 대응하는 장치다.
13. **에이전트 관측성.** Agent Control Standard와 OpenTelemetry 추적은 표준이 아직 초기 단계이므로 시범 적용한다.

## 8. 확인되지 않은 사항

- **OWASP LLM 2026 항목명.** CSA 정리본으로만 확인했다.
- **ForecastBench 실시간 리더보드 수치.** 동적 페이지라 직접 확인하지 못했다.
- **data.go.kr 엔드포인트 세부.** 포털 페이지 접근이 막혀 제공기관 가이드와 공개 사례 기준으로 구현했다.
- **ISO/IEC 42006 발행일, 과기정통부 투명성 지침 원문.** 미확인이다.
