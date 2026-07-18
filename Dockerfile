FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY guardian/ guardian/
COPY sdk/ sdk/
COPY demo/ demo/
COPY policies.yaml .
ENV GUARDIAN_DB=/tmp/guardian.db PYTHONUNBUFFERED=1
EXPOSE 8090
CMD ["uvicorn", "guardian.main:app", "--host", "0.0.0.0", "--port", "8090"]
