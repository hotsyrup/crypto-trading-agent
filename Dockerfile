FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt
RUN useradd --create-home botuser

COPY --chown=botuser:botuser app ./app

RUN mkdir /app/data \
    && chown botuser:botuser /app/data

USER botuser

CMD ["python", "-m", "app.run_paper_bot"]
