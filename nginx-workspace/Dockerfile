# syntax=docker/dockerfile:1

FROM nginx:bookworm AS build

RUN apt-get update && apt-get install -y --no-install-recommends \
    git build-essential ca-certificates \
    libpcre2-dev libssl-dev zlib1g-dev \
    curl pkg-config cmake \
    clang llvm-dev libclang-dev \
    && rm -rf /var/lib/apt/lists/*

# Install rust for nginx
RUN curl -sSf https://sh.rustup.rs | sh -s -- -y \
    && /root/.cargo/bin/rustup default stable
ENV PATH=/root/.cargo/bin:$PATH

# Fetch sources
ADD https://github.com/nginx/nginx.git#release-1.29.1 /nginx
ADD https://github.com/nginx/nginx-acme.git#v0.1.1 /nginx-acme
ADD https://github.com/nginx-modules/ngx_http_hmac_secure_link_module.git#master /ngx_http_hmac_secure_link_module

WORKDIR /nginx

RUN export LIBCLANG_PATH="$(llvm-config --libdir)" \
    && auto/configure \
    --with-compat \
    --with-http_ssl_module \
    --add-dynamic-module=/nginx-acme \
    --add-dynamic-module=/ngx_http_hmac_secure_link_module \
    && make -j"$(nproc)" modules

FROM nginx:bookworm

COPY --from=build /nginx/objs/ngx_http_acme_module.so /usr/lib/nginx/modules/
COPY --from=build /nginx/objs/ngx_http_hmac_secure_link_module.so /usr/lib/nginx/modules/

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip \
    && rm -rf /var/lib/apt/lists/*

COPY nginx-workspace/nginx.conf /etc/nginx/nginx.conf
COPY nginx-workspace/conf.d /etc/nginx/conf.d
COPY nginx-workspace/templates /etc/nginx/templates
COPY nginx-workspace/00-dojo-env.sh /docker-entrypoint.d/00-dojo-env.sh
COPY workspace_job_proxy /opt/workspace_job_proxy
RUN pip3 install --no-cache-dir -r /opt/workspace_job_proxy/requirements.txt
COPY workspace_job_proxy/start-job-proxy.sh /docker-entrypoint.d/10-job-proxy.sh
RUN chmod +x /docker-entrypoint.d/10-job-proxy.sh
ENV PYTHONUNBUFFERED=1 PYTHONPATH=/opt

VOLUME ["/var/cache/nginx/acme-letsencrypt"]

EXPOSE 80
EXPOSE 443

CMD ["nginx", "-g", "daemon off;"]
