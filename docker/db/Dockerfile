# PostgreSQL 16 with the pgvector extension, for local development.
# Built from the postgres:16 image plus the pgvector package from the
# official PostgreSQL apt repository (already configured in that image).
FROM postgres:16

# Only the PostgreSQL repository's package list is downloaded: pgvector
# needs nothing from Debian's repositories, and skipping them keeps the
# build small and quick.
RUN apt-get update \
        -o Dir::Etc::sourcelist=/etc/apt/sources.list.d/pgdg.list \
        -o Dir::Etc::sourceparts=- \
        -o APT::Get::List-Cleanup=0 \
    && apt-get install -y --no-install-recommends postgresql-16-pgvector \
    && rm -rf /var/lib/apt/lists/*
