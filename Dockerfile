# Data Formulator completo + WhatsApp — imagem para Oracle/VPS.
FROM python:3.11.9-slim

# libgomp1: usado por scikit-learn/scipy. Demais dependências são wheels.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOST=0.0.0.0 \
    PORT=8000 \
    DATA_FORMULATOR_HOME=/data

WORKDIR /app

# Instala as dependências primeiro (camada em cache enquanto não mudam).
COPY requirements-render.txt ./
RUN pip install --upgrade pip && pip install -r requirements-render.txt

# Código da aplicação.
COPY servidor_online.py servidor_chatbot.py whatsapp_extension.py \
     whatsapp_agent.py whatsapp_store.py google_sheets_data_loader.py ./

# Diretório persistente (montado como volume no docker-compose).
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

# Um único worker (a fila SQLite e o worker do WhatsApp exigem processo único).
# Forma shell p/ expandir ${PORT}: usa a porta injetada pelo host (ex.: Hyze) e
# cai para 8000 quando PORT não está definido (Oracle/docker-compose).
CMD gunicorn --workers 1 --threads 4 --timeout 300 \
    --bind 0.0.0.0:${PORT:-8000} servidor_online:app
