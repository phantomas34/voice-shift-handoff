# task_app API — Python / FastAPI

Replaces the Node/Express `server.js`. Same routes, same JSON, same tenant
isolation. The React frontend and the PostgreSQL database are unchanged.

## What maps to what

| Node (old)            | Python (new)            |
|-----------------------|-------------------------|
| `server.js`           | `main.py`               |
| `package.json`        | `requirements.txt`      |
| Express               | FastAPI                 |
| `pg` Pool             | `psycopg` + `psycopg_pool` |
| `aws-jwt-verify`      | `PyJWT` + JWKS client   |
| `npm start`           | `uvicorn main:app`      |
| `withTenant()`        | `with_tenant()`         |

The Vite dev proxy still points at `:3000`, so nothing on the frontend changes.

## Run (stub mode — the default)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env          # then edit if needed
PGUSER=task_app_api .venv/bin/uvicorn main:app --port 3000
```

`PGUSER=task_app_api` is not optional — connecting as the table owner bypasses
RLS and the tenant isolation becomes silently fake. Same rule as the Node build.

## Validate

With the API running on :3000 and your seeded database:

```bash
API=http://localhost:3000 bash validate_api.sh
```

Expect `8 passed, 0 failed — ALL GREEN`.

## Switch to Cognito (item 5)

Set these in `.env` and restart:

```
AUTH_MODE=cognito
COGNITO_USER_POOL_ID=us-east-2_xxxxxxxxx
COGNITO_CLIENT_ID=xxxxxxxxxxxxxxxxxxxxxxxxxx
```

The verification path (`_verify_cognito`) is already written — it fetches the
pool's JWKS, checks the RS256 signature, issuer, and audience, and reads
`custom:tenant_id` off the ID token. No code change needed to turn it on.
