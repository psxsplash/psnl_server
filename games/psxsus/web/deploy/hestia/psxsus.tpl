#=========================================================================#
# PSXSUS spectator - HestiaCP nginx template (HTTP)                       #
#                                                                         #
# Install:                                                                #
#   cp psxsus.tpl psxsus.stpl \                                           #
#      /usr/local/hestia/data/templates/web/nginx/php-fpm/                #
#   Then in Hestia: WEB -> the domain -> Web Template = "psxsus".          #
#   Proxy Support can stay OFF; this template IS the proxy.               #
#                                                                         #
# Both files must exist, even if you only serve HTTP: Hestia refuses a    #
# template that has no .stpl sibling the moment SSL is switched on.       #
#                                                                         #
# The backend is hardcoded below. Hestia templates take no custom         #
# variables, so if the spectator does not listen on 127.0.0.1:8089, edit  #
# `proxy_pass` in BOTH files and run `v-rebuild-web-domains <user>`.      #
#=========================================================================#

server {
    listen      %ip%:%web_port%;
    server_name %domain_idn% %alias_idn%;

    # The spectator serves its own assets, so this root exists only for the
    # ACME challenge below and for Hestia's error pages.
    root        %docroot%;

    access_log  /var/log/nginx/domains/%domain%.log combined;
    access_log  /var/log/nginx/domains/%domain%.bytes bytes;
    error_log   /var/log/nginx/domains/%domain%.error.log error;

    include %home%/%user%/conf/web/%domain%/nginx.forcessl.conf*;

    # LET'S ENCRYPT MUST WIN OVER THE PROXY.
    #
    # `^~` so this beats any regex location, and it is served off disk rather
    # than proxied: the spectator knows nothing about ACME and would answer 404,
    # which fails issuance AND every renewal ninety days later - long after
    # anyone remembers touching this file.
    location ^~ /.well-known/acme-challenge/ {
        alias %home%/%user%/web/%domain%/public_html/.well-known/acme-challenge/;
        try_files $uri =404;
    }

    location /error/ {
        alias %home%/%user%/web/%domain%/document_errors/;
    }

    # --- the live feeds ---------------------------------------------------
    #
    # Server-Sent Events. Every default nginx has for a normal response is
    # wrong for a stream, and all four of these matter:
    #
    #   proxy_buffering off   nginx otherwise holds the response until its
    #                         buffer fills, so the page shows nothing for
    #                         several seconds and then jumps - it looks exactly
    #                         like a frozen server. (The app also sends
    #                         `X-Accel-Buffering: no`; this is the belt to that
    #                         pair of braces, and it survives someone removing
    #                         the header.)
    #   proxy_http_version    1.0 has no chunked transfer, which an endless
    #                         response needs.
    #   Connection ""         clears the hop-by-hop `close` nginx would send,
    #                         keeping the upstream connection open.
    #   gzip off              a compressor buffers to find something to
    #                         compress, which is the one thing a stream cannot
    #                         afford.
    #
    # `proxy_read_timeout` is the wall clock nginx allows between BYTES, not the
    # length of the stream. The room feed pushes at 15Hz and the lobby feed at
    # 1Hz, so anything over a few seconds is enough; an hour is set so that a
    # future quiet feed does not get cut off mid-round.
    location ^~ /api/stream/ {
        proxy_pass http://127.0.0.1:8089;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        proxy_buffering off;
        proxy_cache off;
        gzip off;
        chunked_transfer_encoding off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }

    # --- everything else --------------------------------------------------
    location / {
        proxy_pass http://127.0.0.1:8089;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header Host $host;
        # X-Real-IP is what the spectator reads to decide whether the person
        # browsing is also sitting at a console. It only counts when
        # PSXSUS_WEB_TRUST_PROXY names this proxy's address - see SPECTATOR.md.
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        proxy_read_timeout 60s;

        # The whole application is read-only. Nothing it serves has ever needed
        # a request body, so anything else is refused before it reaches Python.
        limit_except GET HEAD {
            deny all;
        }
    }

    location ~ /\.(?!well-known\/) {
        deny all;
        return 404;
    }

    include %home%/%user%/conf/web/%domain%/nginx.conf_*;
}
