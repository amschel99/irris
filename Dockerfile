# 1. the dashboard
FROM node:24-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json* ./
RUN npm ci || npm install
COPY web/ ./
RUN npm run build

# 2. the server
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY irris/ irris/
COPY site/ site/
COPY docs/ docs/
COPY --from=web /web/dist web/dist
EXPOSE 8000
CMD ["python", "-m", "irris"]
