# cors_policy

Canonical, versioned CORS + environment schema shared by the two MAP Python
services in this monorepo:

- `map_core` (algorithm-service)
- `map-business-backend` (BFF / worker-service)

The single source of truth is `cors_policy.py`. It is vendored VERBATIM into
both services (the two services are separate uv projects with their own
lockfiles and Docker build contexts, so a path-dependency package would add
lockfile/build-context churn without changing runtime behavior):

```
map_core/map_core/utils/cors_policy.py          (copy)
map-business-backend/app/cors_policy.py         (copy)
```

Never edit a vendored copy directly. Edit `packages/cors_policy/cors_policy.py`,
bump `CORS_POLICY_VERSION`, then copy it to both services. Each service has a
parity test (`test_cors_schema.py`) that fails if its local copy is not
byte-identical to this file.

## Contract

- origins: `*` or `http(s)://host[:port]` with port in 1..65535 and no
  userinfo / path / query / fragment (parsed with `urllib.parse.urlsplit`).
- booleans: strict enum `true` / `false` / `1` / `0` (case-insensitive).
- environment: strict enum `dev` / `test` / `pre` / `prod`; unknown fails closed.
- production (`prod`) refuses wildcard origin + credentials at startup.
