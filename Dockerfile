FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /uvx /bin/

WORKDIR /app
# UV_NO_SYNC: `uv run` inside the container uses the baked --no-dev env
# instead of re-syncing (and pulling dev deps) at runtime.
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_NO_SYNC=1

# Dependency layer (cached until the lockfile changes).
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY alembic.ini ./
COPY migrations ./migrations
RUN uv sync --frozen --no-dev

# The venv's scripts on PATH, so `docker compose exec app arb ...` (the form
# the docs promise) finds the console script. After the syncs on purpose: it
# costs no cache, and the build itself never needed it.
ENV PATH="/app/.venv/bin:$PATH"

CMD ["uv", "run", "arb"]
