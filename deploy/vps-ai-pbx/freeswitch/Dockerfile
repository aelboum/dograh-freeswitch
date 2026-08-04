# syntax=docker/dockerfile:1
#
# FreeSWITCH 1.10.12 + mod_audio_stream (aelboum/mod_audio_stream v1.0.0-ai-pbx),
# built from source on Debian 12 (bookworm), matching the live Raspberry Pi
# install this deployment is migrated from — see ../RASPBERRY_FREESWITCH_BACKUP.md
# for the audit this Dockerfile is derived from.
#
# Target platform: linux/amd64 (VPS). No architecture-specific flags are used,
# so `docker buildx build --platform linux/arm64 .` remains possible later for
# the Raspberry Pi — not built/tested as part of this migration.
#
# Treated as an immutable runtime artifact: nothing in the final image is
# expected to be hand-edited after boot. Config that varies by deployment is
# rendered from environment variables at container start (docker-entrypoint.sh),
# not baked in here.

ARG DEBIAN_FRONTEND=noninteractive

# ---------------------------------------------------------------------------
# Stage 1: build spandsp + sofia-sip from source.
#
# Debian bookworm's own libspandsp-dev package is 0.0.6 (pre-3.0 versioning);
# FreeSWITCH 1.10.12 requires spandsp >= 3.0. sofia-sip isn't packaged for
# Debian at all. Both must be built from source — confirmed on the live Pi
# and independently hit by aelboum/mod_audio_stream's own reproducibility CI
# when it first tried apt's libspandsp-dev. Commits pinned to exactly what's
# running on the live Pi today.
# ---------------------------------------------------------------------------
FROM debian:12-slim AS deps-builder

ARG SPANDSP_COMMIT=0d2e6ac65e0e8f53d652665a743015a88bf048d4
ARG SOFIA_SIP_COMMIT=7333682a297f28130ff46d5ef0f9e2f72adca0e8

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential git autoconf automake libtool libtool-bin pkg-config \
        libtiff-dev ca-certificates \
        libssl-dev zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /usr/src
RUN git clone https://github.com/freeswitch/spandsp.git spandsp-src \
    && cd spandsp-src && git checkout ${SPANDSP_COMMIT} \
    && ./bootstrap.sh && ./configure --prefix=/usr && make -j"$(nproc)" && make install

RUN git clone https://github.com/freeswitch/sofia-sip.git sofia-sip-src \
    && cd sofia-sip-src && git checkout ${SOFIA_SIP_COMMIT} \
    && ./bootstrap.sh && ./configure --prefix=/usr --with-glib=no && make -j"$(nproc)" && make install

# ---------------------------------------------------------------------------
# Stage 2: build FreeSWITCH itself with the exact module set audited on the
# live Pi (RASPBERRY_FREESWITCH_BACKUP.md's "Loaded modules" section).
# ---------------------------------------------------------------------------
FROM debian:12-slim AS freeswitch-builder

ARG FREESWITCH_COMMIT=a88d069d6ffb74df797bcaf001f7e63181c07a09

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential git autoconf automake libtool libtool-bin pkg-config ca-certificates \
        libssl-dev libcurl4-openssl-dev libpcre3-dev libspeex-dev libspeexdsp-dev \
        libldns-dev libedit-dev libtiff-dev uuid-dev zlib1g-dev libjpeg62-turbo-dev \
        libsqlite3-dev libpq-dev libopus-dev libsndfile1-dev python3 yasm \
    && rm -rf /var/lib/apt/lists/*

# spandsp/sofia-sip built in stage 1, installed under /usr
COPY --from=deps-builder /usr/lib /usr/lib
COPY --from=deps-builder /usr/include /usr/include
COPY --from=deps-builder /usr/lib/pkgconfig /usr/lib/pkgconfig
RUN ldconfig

WORKDIR /usr/src
RUN git clone https://github.com/signalwire/freeswitch.git freeswitch \
    && cd freeswitch && git checkout ${FREESWITCH_COMMIT}

# Module list matches RASPBERRY_FREESWITCH_BACKUP.md's audited modules.conf.xml
# exactly (mod_xml_curl deliberately excluded — it's unused today, see
# ARCHITECTURE.md §4b for why it's the future Configuration Service's seam,
# not something wired up in this deployment).
COPY freeswitch-modules.conf /usr/src/freeswitch/modules.conf

WORKDIR /usr/src/freeswitch
RUN ./bootstrap.sh -j \
    && ./configure --prefix=/usr/local/freeswitch \
    && make -j"$(nproc)" \
    && make install

# ---------------------------------------------------------------------------
# Stage 3: build mod_audio_stream from the already-reproducible, CI-built
# aelboum/mod_audio_stream fork (v1.0.0-ai-pbx) against the FreeSWITCH headers
# from stage 2 — not a fresh rebuild of the community module.
# ---------------------------------------------------------------------------
FROM freeswitch-builder AS mod-audio-stream-builder

RUN apt-get update && apt-get install -y --no-install-recommends \
        cmake libwebsockets-dev libevent-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /usr/src
RUN git clone --recurse-submodules --branch v1.0.0-ai-pbx \
        https://github.com/aelboum/mod_audio_stream.git

# USE_TLS=ON is required for wss:// through the Cloudflare tunnel — confirmed
# via readelf -d against the live Pi's production .so (RASPBERRY_FREESWITCH_BACKUP.md).
# A default (USE_TLS=OFF) build looks identical at build time and only fails
# at actual wss:// connect time, so this flag is never optional here.
RUN cd mod_audio_stream && mkdir build && cd build \
    && PKG_CONFIG_PATH=/usr/local/freeswitch/lib/pkgconfig \
       cmake -DCMAKE_BUILD_TYPE=Release -DUSE_TLS=ON .. \
    && make -j"$(nproc)" \
    && make install

# ---------------------------------------------------------------------------
# Stage 4: runtime image — only what's needed to run the built FreeSWITCH
# tree, none of the compilers/headers from the build stages.
# ---------------------------------------------------------------------------
FROM debian:12-slim AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
        libssl3 libcurl4 libpcre3 libspeex1 libspeexdsp1 libldns3 libedit2 \
        libtiff6 uuid-runtime zlib1g libjpeg62-turbo libsqlite3-0 libpq5 \
        libopus0 libsndfile1 \
        libwebsockets17 libevent-2.1-7 libevent-pthreads-2.1-7 libevent-openssl-2.1-7 gettext-base ca-certificates \
        findutils mawk \
    && rm -rf /var/lib/apt/lists/*

# spandsp/sofia-sip runtime shared libs (built in stage 1)
COPY --from=deps-builder /usr/lib/libspandsp* /usr/lib/
COPY --from=deps-builder /usr/lib/libsofia* /usr/lib/
# Full FreeSWITCH install tree, including mod_audio_stream.so installed into it
COPY --from=mod-audio-stream-builder /usr/local/freeswitch /usr/local/freeswitch
RUN ldconfig

# Bootstrap config templates, rendered at container start — see
# docker-entrypoint.sh and ARCHITECTURE.md §4a for the bootstrap-vs-tenant-
# runtime-config split this implements.
COPY conf/ /usr/local/freeswitch/conf.templates/
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

ENV PATH="/usr/local/freeswitch/bin:${PATH}"

# SIP + RTP must be published on the host (see ../VPS_FIREWALL.md). RTP
# range is documentation-only here (the real range is set at compose level
# via RTP_PORT_RANGE_START/END, see docker-compose.vps.yml and
# docker-entrypoint.sh) — 20000-20199 is just the default.
# 8021 (ESL) is intentionally NOT exposed here — reached only via the
# internal Docker network by service name (see docker-compose.vps.yml).
EXPOSE 5060/udp 5060/tcp 20000-20199/udp

# Fallback healthcheck for standalone use (e.g. plain `docker run`) —
# docker-compose.vps.yml defines its own equivalent healthcheck for this
# service, which takes precedence whenever the image runs via that compose
# file (compose-level healthcheck always overrides the image's own
# HEALTHCHECK). Shell form (not JSON-array CMD) so $ESL_PASSWORD expands —
# fs_cli defaults to password "ClueCon" otherwise, which fails auth against
# any real (non-default) ESL_PASSWORD and reports as a connection error
# indistinguishable from ESL actually being down.
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD fs_cli -p "$ESL_PASSWORD" -x "status" || exit 1

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
# Deliberately NOT -nc (no-console/daemonize): that flag forks FreeSWITCH to
# the background and exits the foreground process immediately, which is
# exactly wrong in a container — Docker tracks the foreground process as
# the container's lifetime, so `-nc` makes the container exit (and, under
# `restart: unless-stopped`, loop-restart) the instant it starts. Running
# in the default foreground/console mode keeps the container's PID 1 alive
# and sends FreeSWITCH's console log straight to `docker logs`.
CMD ["/usr/local/freeswitch/bin/freeswitch", "-nonat"]
