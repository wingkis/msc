FROM python:3.12-bookworm

# Use Aliyun mirror for faster arm64 wheel downloads
ENV PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
ENV PIP_TRUSTED_HOST=mirrors.aliyun.com
# Container logs are the audit trail here, so nothing may sit in a Python buffer.
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY msc/ msc/
COPY ui/ ui/

RUN mkdir -p /app/data /app/backups /home/astro/.msc/memory /home/astro/.claude/projects/-home-astro/memory

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/v1/health')" || exit 1

CMD ["python", "-m", "msc.main"]
