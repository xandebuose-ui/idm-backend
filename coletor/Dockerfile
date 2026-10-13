# Coletor de conversa (IDM Escuta) — navegador real headless
# Imagem oficial do Playwright: já traz Chromium + todas as libs de sistema.
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

ENV PYTHONUNBUFFERED=1
# Render injeta PORT; usamos 10000 como padrão local.
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-10000}"]
