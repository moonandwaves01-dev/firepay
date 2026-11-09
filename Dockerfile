FROM python:3.12-slim

WORKDIR /app
COPY backend /app/backend

ENV BACKEND_HOST=0.0.0.0
ENV BACKEND_PORT=10000

EXPOSE 10000
CMD ["python", "backend/server.py"]
