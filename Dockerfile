FROM python:3.12-slim

WORKDIR /app

# Dipendenze di sistema minime
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Installa librerie Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia lo script
COPY monitor.py .

# Utente non-root. L'UID/GID 1000 coincide con quello del proprietario di
# ./data sull'host: così il volume è scrivibile senza dover far girare il
# container da root (era il motivo del `user: "0:0"` nel compose).
RUN useradd -u 1000 -U -m -s /usr/sbin/nologin appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app
USER appuser

CMD ["python", "-u", "monitor.py"]
