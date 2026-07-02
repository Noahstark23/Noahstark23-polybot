# HANDOFF — Estado del proyecto

Fase activa: **F0 — Scaffold & Discovery**
Gate pendiente: F0 -> F1 (ver ARCHITECTURE.md §6)

## Checklist de la fase actual
- [ ] docker build verde, contenedor healthy >= 1h en Coolify (:18081/health)
- [ ] GET /health 200, GET /status devuelve JSON con started_at
- [ ] smoke_test.py verde en CI
- [ ] pytest de config y risk/manager al 100%
- [ ] .pem/keystore fuera del repo; .gitignore + .dockerignore correctos

## Última iteración
2026-07-02 — Archivos fundacionales creados (ARCHITECTURE.md, GOAL.md, CLAUDE.md,
PROJECT_LOOP.md, Dockerfile, docker-compose.yml, pyproject.toml, .env.example,
.gitignore, .dockerignore, CI). Aún no hay código en `src/`.

## Siguiente paso
Portar infra base del repo botkalshi y levantar el health server sin motores.
