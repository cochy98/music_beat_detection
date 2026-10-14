FROM node:22-bookworm-slim AS build
WORKDIR /app
COPY package.json package-lock.json ./
COPY api/package.json api/package.json
COPY web/package.json web/package.json
RUN npm ci
COPY shared ./shared
COPY web ./web
RUN npm run build --workspace web

FROM nginxinc/nginx-unprivileged:stable-alpine
USER root
ARG SECURITY_REFRESH=manual
RUN apk upgrade --no-cache
USER 101
COPY web/security-headers.conf /etc/nginx/security-headers.conf
COPY web/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/web/dist /usr/share/nginx/html
EXPOSE 8080
