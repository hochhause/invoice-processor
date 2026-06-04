FROM python:3.11-slim

# pdfplumber needs poppler for some PDF types; markitdown needs it too
RUN apt-get update && apt-get install -y --no-install-recommends \
    poppler-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY start.sh .
RUN chmod +x start.sh

COPY app/ .

EXPOSE 8000

CMD ["./start.sh"]
