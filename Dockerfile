FROM python:3.12-slim

WORKDIR /app
COPY . .

RUN if [ -f requirements.txt ]; then pip install --no-cache-dir -r requirements.txt; fi

ENV HOST=0.0.0.0
ENV PORT=8080

EXPOSE 8080
CMD ["python", "server.py"]
