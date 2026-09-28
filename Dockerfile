# PSNL server.
#
#   docker build -t psnl-server .
#   docker compose up --build
#
# BUILT FROM ITS OWN ROOT. This used to require building from the directory ABOVE
# the repo, because the wire package and the game lived outside it, and pip needed
# --no-deps and --find-links to be talked out of fetching a `psnl` from PyPI that
# has never existed there. A repo that cannot be built from a clone of itself is
# not forkable, so all of it lives here now and this is an ordinary build.

FROM python:3.12-slim AS build

WORKDIR /build
RUN python -m pip install --no-cache-dir --upgrade pip build
COPY . .
RUN python -m build --wheel --outdir /wheels \
 && python -m pip wheel --no-cache-dir --wheel-dir /wheels uvloop


FROM python:3.12-slim

# Never run a network service as root.
RUN useradd --create-home --uid 10001 psnl

COPY --from=build /wheels /wheels
RUN python -m pip install --no-cache-dir --find-links=/wheels "psnl[fast]" \
 && rm -rf /wheels

# Created HERE, owned by psnl, and before the USER switch.
#
# A named volume mounted over an existing directory inherits that directory's
# ownership from the image; mounted over one that does not exist it is created
# root-owned, and a service running as uid 10001 then cannot write to its own
# data directory. The spectator's stats file is the only thing that lands here,
# and it fails soft - so the symptom would have been stats that silently never
# persisted rather than anything crashing.
RUN mkdir -p /data && chown psnl:psnl /data
VOLUME /data

USER psnl
WORKDIR /home/psnl

ENV PSNL_HOST=0.0.0.0 \
    PSNL_RAW_PORT=6699 \
    PSNL_PROTOBUF_PORT=6700 \
    PSNL_LOG_LEVEL=INFO \
    PYTHONUNBUFFERED=1

# 6699 raw (the Python bridge, i.e. real hardware), 6700 protobuf (PCSX-Redux),
# 8089 PSXSUS's spectator web view - which only listens when PSXSUS_WEB_PORT is
# set, and which is unauthenticated when it does. See games/psxsus/SPECTATOR.md.
EXPOSE 6699 6700 8089

# Prove the listener actually accepts a connection rather than that the process
# exists: a wedged event loop still has a live PID.
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import socket,os; s=socket.create_connection(('127.0.0.1', int(os.environ['PSNL_RAW_PORT'])), 3); s.close()" || exit 1

# No game by default. Without --game (or PSNL_GAME) this is a pure relay, which is
# a useful thing to be: two consoles sharing one scene need nothing more.
ENTRYPOINT ["python", "-m", "psnl_server"]
