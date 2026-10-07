# Base image only. Tectonic, Tesseract and Playwright are added in the releases that need them (PLAN.md).
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --locked --no-dev --no-install-project

COPY src ./src
COPY templates ./templates
RUN uv sync --locked --no-dev

RUN useradd --create-home --uid 1000 jobradar && mkdir -p /app/data /app/output /app/config \
    && chown -R jobradar:jobradar /app
USER jobradar

ENV PATH="/app/.venv/bin:$PATH"
ENTRYPOINT ["jobradar"]
CMD ["run"]
