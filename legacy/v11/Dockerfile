# Use 3.10-slim for balance of size and compatibility
FROM python:3.10-slim

WORKDIR /app

# Set env vars
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app:/app/src

# Install system dependencies
# cv2 needs libglib2.0-0 and libgl1-mesa-glx even for headless sometimes
# build-essential for compiling some python extensions
RUN apt-get update && apt-get install -y \
    gcc \
    build-essential \
    libglib2.0-0 \
    libgl1 \
    libglx-mesa0 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY . .

# Default command (overridden by compose)
CMD ["tail", "-f", "/dev/null"]
