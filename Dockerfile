FROM python:3.11-bookworm

# poppler-utils, libzbar0 for PDF/QR; mesa libs for Docling OCR in headless env
RUN apt-get update && apt-get install -y --no-install-recommends \
    poppler-utils \
    libzbar0 \
    libgl1-mesa-glx \
    libxext6 \
    libxrender1 \
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
