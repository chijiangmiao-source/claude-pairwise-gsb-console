ARG BASE_IMAGE=claude-eval-runtime:claude-2.1.269
FROM ${BASE_IMAGE}

USER root
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
       build-essential ca-certificates cmake file lsof netcat-openbsd \
       pkg-config procps psmisc python3-dev python3-pip python3-venv \
       sqlite3 tree zip \
    && rm -rf /var/lib/apt/lists/*
RUN npm install --global pnpm@9.15.9

USER node
