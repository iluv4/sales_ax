FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY ax ax
COPY web web
COPY vendor vendor
COPY run.py .
# Customer data lives on the mounted volume, never in the image.
ENV SALES_AX_DB=/data/sales-ax.db SALES_AX_PRIVATE_DIR=/data/private
CMD ["python","run.py"]
