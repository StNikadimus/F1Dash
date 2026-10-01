FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py ./
COPY server ./server
COPY dashboard ./dashboard
COPY tools ./tools
COPY config ./config
COPY data ./data

RUN useradd --create-home --uid 1000 f1 && chown -R f1:f1 /app
USER f1

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=4).status == 200 else 1)"

CMD ["python", "main.py"]
