# Davi Barros de Rezende

**Senior Software Engineer — Generative AI & mission-critical systems.**
TypeScript · Node/NestJS · React · Python · PostgreSQL · Vitória, Brazil (UTC−3) · Remote

I build the layer that makes LLMs survive production: gateways with fallback budgets,
deterministic guardrails, and eval benches that pick a model on quality-per-dollar.
I got there from the other side — SCADA, sensor fusion, Kalman filters — which is why
I care more about *how a number was measured* than about how good it looks.

---

### What the git history actually shows

| | |
|---|---|
| **OOInfo** — [ooinfo.org](https://ooinfo.org), live | Tech Lead / Owner (recorded in `CONTRIBUTING.md`). 1,531 of 2,853 commits across 18 authors, including the founding one. 495 REST endpoints · 76 tables · 131 migrations · 13 queues. |
| **Brametal SCADA** — private, proprietary license | 368 of 368 commits. Nine months, sole author, first commit to signed installer. |
| **Orbya** — B2B revenue intelligence | 398 of 428 commits (93%). ~100k lines of production code in 3.5 months. |
| **emf_rover** — RTK field robot | 364 of 364 commits. 41k lines of Python, 15-state Extended Kalman Filter. |

---

### Selected work

**MCP server for a live data platform** *(OOInfo — 33 of the module's 37 commits)*
15 JSON-RPC tools behind OAuth 2.1 with Dynamic Client Registration. Personal tokens are stored
only as SHA-256 under a prefix chosen so secret scanners recognise a leak. The four scopes can
only *narrow* the existing access-control layer — never widen it — and the tool documentation is
generated from the real catalogue and fails CI when it drifts.

**Generative-AI layer** *(OOInfo — 132 of the module's 226 commits, ~16.6k lines)*
Provider-agnostic gateway (DeepSeek / OpenRouter / OpenAI) with a three-step fallback cascade under
a **global** time budget, so three upstream timeouts don't stack up in front of the user. SSE
streaming with an incremental JSON parser, an agentic tool loop, audited quota, and six upstream
failure modes modelled as domain types. No orchestration framework — it's all hand-rolled.

**Multi-tenant isolation defended in the database** *(Orbya)*
Row-Level Security across 18 tables, 6 PL/pgSQL functions, application role without `BYPASSRLS` —
forget the scope and you get zero rows instead of another tenant's data. Proved with an automated
cross-tenant IDOR test running against real Postgres on every PR. Queue with atomic claim,
`pg_notify` + `LISTEN` + SSE — no Redis, no WebSocket broker.

**Deterministic anti-hallucination gate** *(Orbya)*
Every number an LLM writes is matched against an index of values actually present in the context it
received. Fails the match, the insight doesn't publish. Alongside an LLM-as-judge eval bench
(4 dimensions, golden set of real fixtures) that elects the production model by quality-per-dollar:
the cheapest candidate that still clears the baseline.

**Fail-safe boundary between software and machine** *(Brametal SCADA)*
Reverse-engineered a 15 KB PLC memory contract byte by byte from third-party TIA Portal exports,
proving each UDT by CRC rather than by name similarity. The heartbeat runs in a separate Node
process with its own S7 socket, flipping one bit every 100 ms — if the backend dies, the PLC cuts
traction on its own. A test parses the control engineer's own export and fails any divergent offset;
it caught an address error that would have killed the safety heartbeat during a live test.

**Curvature-spectrum descriptor, and a public retraction** *(Signfy)*
Orthonormal DCT-II over curvature κ(s) reparametrised by arc length, using the fundamental theorem
of plane curves as a falsification test rather than a feature. Then a 37-agent adversarial audit
across 4 independent lenses (46 findings, 22 cross-verified) that killed a headline number I had
already published — it was measuring the second-derivative operator, not the signature. I published
the retraction with the same prominence as the original result.

**Upstream contribution** — [lowcodejs](https://lowcodejs.org)
Designed the `RowAccessGuard` row-level authorisation plugin contract: six methods, a three-valued
allow/deny/abstain algebra composed across independent guards, and ACL filter pushdown into the
MongoDB query so pagination and counts don't break. Delivered by fork + PR #182, merged upstream.
The interface is still on `main` today.

---

### Stack

**Languages** TypeScript · JavaScript · Python 3 · SQL / PL/pgSQL · C++ (ESP32) · Bash
**AI in production** MCP (OAuth 2.1, scopes) · agents & tool calling · multi-agent systems ·
provider-agnostic LLM gateways · structured output over JSON Schema · LLM-as-a-judge & evals ·
hallucination mitigation · per-tenant cost ceilings · ONNX Runtime · scikit-learn
**Backend** Node 20/22 · NestJS · Fastify · Next.js (App Router, Server Actions) · REST · OpenAPI ·
JSON-RPC · idempotent webhooks · BullMQ · Postgres-backed job queues · SSE · WebSocket
**Data** PostgreSQL 16 · Prisma · Row-Level Security · PL/pgSQL · full-text search (tsvector,
pg_trgm, unaccent, GIN) · keyset pagination · `FOR UPDATE SKIP LOCKED` · Redis · InfluxDB · MongoDB
**Frontend** React 18/19 · React Native + Expo · Vite · TanStack Query · Zustand · Tailwind · Tauri
**Infra & quality** Docker · GitHub Actions · GHCR pull-based deploys · Coolify · Caddy/Nginx ·
Vitest · Jest · Cypress · Playwright · pytest · chaos & soak testing · integration tests against a
real database in CI
**Critical & embedded** *(where the measurement discipline comes from)* SCADA · Siemens S7-1500 ·
S7Comm · binary protocol reverse engineering · fail-safe design & watchdogs · GNSS-RTK · NTRIP ·
RTCM3 · u-blox ZED-F9P · sensor fusion · Extended Kalman Filter · Bayesian optimisation · DCT-II

---

Most of the work above lives in private repositories — client-owned or under proprietary license.
Happy to walk through architecture and trade-offs in a call.

[LinkedIn](https://www.linkedin.com/in/davi-rezende-09540b222/) · davidbecam006@gmail.com

<sub>Engenheiro de software brasileiro. Trabalho com IA generativa em produção e sistemas críticos —
e vim da engenharia, que é de onde tirei a mania de perguntar como o número foi medido.</sub>
