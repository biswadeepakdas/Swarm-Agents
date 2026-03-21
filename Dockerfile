FROM python:3.12-slim

WORKDIR /app

# Install system deps for httpx/search
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    python -c "from googlesearch import search; print('googlesearch OK')" && \
    python -c "import httpx; print('httpx OK')"

COPY . .

EXPOSE 8000
CMD ["python", "-m", "swarm.main"]
