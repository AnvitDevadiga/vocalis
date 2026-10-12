FROM python:3.12-slim
WORKDIR /srv/vocalis
COPY requirements-prod.txt .
RUN pip install --no-cache-dir -r requirements-prod.txt
RUN groupadd --system vocalis && useradd --system --gid vocalis --home-dir /home/vocalis --create-home vocalis \
    && mkdir -p /srv/vocalis/data /home/vocalis/.cache \
    && chown -R vocalis:vocalis /srv/vocalis /home/vocalis
COPY --chown=vocalis:vocalis app.py .
COPY --chown=vocalis:vocalis alembic.ini .
COPY --chown=vocalis:vocalis alembic ./alembic
COPY --chown=vocalis:vocalis vocalis ./vocalis
COPY --chown=vocalis:vocalis static ./static
ENV HOME=/home/vocalis PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER vocalis
EXPOSE 8000
CMD ["python", "-m", "vocalis.start"]
