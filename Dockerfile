FROM python:3.11-slim

# poppler-utils needed for PDF operations; libzbar0 needed for QR decoding
RUN apt-get update && apt-get install -y --no-install-recommends \
    poppler-utils \
    libzbar0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-bake Docling models to avoid long startup delay on first run
RUN python -c "from docling.document_converter import DocumentConverter; DocumentConverter()"

COPY start.sh .
RUN chmod +x start.sh

COPY app/ .

EXPOSE 8000

CMD ["./start.sh"]
