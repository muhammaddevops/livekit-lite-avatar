# The LiteAvatar renderer, CPU only.
#
#   docker build -t livekit-lite-avatar-renderer .
#   docker run -p 8791:8791 -v liteavatar-data:/data -e LITEAVATAR_SECRET=change-me livekit-lite-avatar-renderer
#
# LiteAvatar's code, weights and presenters are not baked into the image (their licences differ; see README,
# "Licences"): the first start downloads them (~1.3 GB) into /data. Mount a volume there to keep them.
FROM python:3.10-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ffmpeg libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 app \
    && mkdir /data && chown app:app /data

WORKDIR /app
COPY pyproject.toml README.md LICENSE constraints-renderer.txt ./
COPY src ./src
# CPU-only torch from PyTorch's index (the default Linux wheels pull in CUDA); versions pinned to the tested set
RUN pip install --no-cache-dir --extra-index-url https://download.pytorch.org/whl/cpu \
        -c constraints-renderer.txt ".[renderer]"

USER app
ENV LITEAVATAR_HOME=/data \
    LITEAVATAR_HOST=0.0.0.0 \
    PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8791
HEALTHCHECK --interval=30s --timeout=5s --start-period=10m \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8791/health', timeout=4)"
ENTRYPOINT ["liteavatar-renderer"]
CMD ["serve", "--download"]
