# Dockerfile — backend image
FROM python:3.11-slim

# Install system deps: ffmpeg (video), fonts (for drawtext + subtitles), build tools
# libgles2 + libegl1: required by MediaPipe Tasks API even in CPU-only mode
# (MediaPipe uses OpenGL ES for pre/post-processing on any backend)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    build-essential \
    curl \
    fonts-noto \
    fonts-noto-cjk \
    fonts-noto-extra \
    fonts-dejavu \
    fonts-liberation \
    libgles2 \
    libegl1 \
    && fc-cache -f \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (better layer caching)
COPY backend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy backend code
COPY backend /app/backend

# Expose FastAPI port
EXPOSE 8000

# Default command (overridden in docker-compose for worker)
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]