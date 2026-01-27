FROM mcr.microsoft.com/playwright/python:v1.41.2-jammy

WORKDIR /app

COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app/

ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
ENV PORT=8080

CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:8080"]
