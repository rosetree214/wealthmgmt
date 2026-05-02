# AGENTS.md

## Repository overview

This repo (`wealthmgmt`) is a monorepo with **two independent products on separate feature branches** (the `main` branch contains only a README):

| Product | Branch | Stack |
|---|---|---|
| **WealthWise** | `cursor/build-premium-wealth-management-web-app-5bd0` | Node.js 20 / Express / Prisma / PostgreSQL 16 (backend) + Next.js 15 / React 19 / Tailwind v4 (frontend) |
| **13F Mirror Trader** | `cursor/13f-mirror-trader-76c3` | Python 3.12 / Streamlit / edgartools / alpaca-py / pytest |

## Cursor Cloud specific instructions

### System dependencies

- **Node.js 20** (via nodesource) — required for WealthWise
- **Docker** — required for PostgreSQL container (WealthWise backend)
- **Python 3.12 + python3.12-venv** — required for 13F Mirror Trader

### WealthWise (backend + frontend)

Files live on branch `cursor/build-premium-wealth-management-web-app-5bd0`. To work on it, check out that branch or cherry-pick files.

- **PostgreSQL**: `docker compose up -d db` (uses `docker-compose.yml` at repo root). Connection: `postgresql://postgres:postgres@localhost:5432/wealthwise`
- **Backend**: `cd backend && npm install && npx prisma migrate dev && npm run dev` (port 4000). Requires `.env` with `DATABASE_URL`, `JWT_SECRET`, `PORT`.
- **Frontend**: `cd frontend && npm install && npm run dev` (port 3000).
- **Known issue**: The original `package.json` has `"type": "module"` which conflicts with `ts-node-dev`. The dev script was changed to use `tsx watch` instead. If you see ESM import errors, ensure `tsx` is installed (`npm install --save-dev tsx`) and the dev script uses `tsx watch src/index.ts`.
- **Lint**: `cd frontend && npx next lint`. Pre-existing unused-variable warnings exist in `components/OnboardingForm.tsx`.
- **Build**: `cd backend && npx tsc` (backend compiles cleanly). Frontend build (`npm run build`) fails due to the lint errors above; dev mode works fine.
- **Auth routes** are stub implementations — register returns `{ ok: true }` but doesn't persist to DB, login always returns 401. This is expected.

### 13F Mirror Trader

Files live on branch `cursor/13f-mirror-trader-76c3`. Use a worktree or check out that branch.

- **Setup**: `python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`
- **Config**: `cp .env.example .env` and set `EDGAR_IDENTITY` (required for SEC API calls).
- **Run**: `streamlit run app.py --server.headless true --server.port 8501`
- **Tests**: `pytest tests/ -v` (26 tests, all passing).
- No lint tool is configured for the Python project.

### Docker in Cloud Agent VMs

The Cloud Agent VM runs inside a container. Docker requires:
1. `fuse-overlayfs` storage driver (configured in `/etc/docker/daemon.json`)
2. `iptables-legacy` (set via `update-alternatives`)
3. Start daemon with `dockerd &>/var/log/dockerd.log &` then wait ~3s before using docker commands.
