FROM python:3.12-alpine
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
COPY templates/ ./templates/
EXPOSE 8080
ENV PORT=8080
CMD ["gunicorn", "-b", "0.0.0.0:8080", "app:app", "--workers", "2"]
