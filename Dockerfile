FROM python:3.12-slim

ARG GRAVELLE_VERSION=source
ARG GRAVELLE_RELEASE_REPOSITORY=pumpkissster/Gravelle
ARG VCS_REF=unknown
ARG SOURCE_URL=

LABEL org.opencontainers.image.title="Gravelle" \
    org.opencontainers.image.description="Private photo library and reader" \
    org.opencontainers.image.version="${GRAVELLE_VERSION}" \
    org.opencontainers.image.revision="${VCS_REF}" \
    org.opencontainers.image.source="${SOURCE_URL}" \
    org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LIBRARY_PATH=/library \
    DATA_PATH=/data \
    HOST=0.0.0.0 \
    PORT=8080 \
    SCAN_INTERVAL=300 \
    GRAVELLE_RELEASE_REPOSITORY=${GRAVELLE_RELEASE_REPOSITORY}

WORKDIR /app

# The PDFium Linux font mapper scans /usr/share/fonts directly. Keep the
# Debian package copyright files alongside the CJK fonts and fontconfig tools.
RUN apt-get update \
    && apt-get install -y --no-install-recommends fontconfig fonts-noto-cjk \
    && fc-cache -f \
    && test -s /usr/share/doc/fonts-noto-cjk/copyright \
    && test -s /usr/share/doc/fontconfig/copyright \
    && python -c "import subprocess; matches=[subprocess.check_output(['fc-match','-f','%{family}','sans-serif:lang='+lang],text=True) for lang in ('zh-cn','zh-tw','ja','ko')]; assert all('Noto' in name and 'CJK' in name for name in matches), 'CJK fallback fonts are missing'" \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 1000 sumi \
    && useradd --uid 1000 --gid sumi --no-create-home \
        --home-dir /nonexistent --shell /usr/sbin/nologin sumi \
    && mkdir -p /library /data \
    && chown sumi:sumi /data

COPY requirements.txt ./requirements.txt
RUN python -m pip install --no-cache-dir -r requirements.txt \
    && python -c "import pypdfium2; pypdfium2.PdfDocument.new().close()" \
    && python -c "from cryptography.hazmat.primitives.ciphers.aead import AESGCM; c=AESGCM(bytes(32)); assert c.decrypt(bytes(12),c.encrypt(bytes(12),b'build-only',b''),b'')==b'build-only'"

COPY server.py accounts.py password_reset.py transcoding.py image_search.py image_search_features.py task_scheduler.py library_covers.py cache_manager.py sharing.py share_crypto.py share_passwords.py epub_reader.py pdf_reader.py ./
COPY desktop/licenses/OpenSSL-4.0.2-LICENSE.txt ./licenses/cryptography/OpenSSL-4.0.2-LICENSE.txt
COPY release.json LICENSE ./
COPY static ./static

RUN python -c "import ast, pathlib; tree=ast.parse(pathlib.Path('server.py').read_text(encoding='utf-8')); version=next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'VERSION' for t in n.targets)); assert '${GRAVELLE_VERSION}' in ('source', version), 'Image version differs from server.VERSION'" \
    && python password_reset.py --help > /dev/null

USER 1000:1000
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.getenv('PORT','8080')+'/api/health', timeout=5).read()"

CMD ["python", "server.py"]
