FROM node:22-slim

ENV NODE_ENV=production \
    CODEX_HOME=/codex-auth \
    HOME=/home/node \
    PATH=/srv/advisor/node_modules/.bin:$PATH

WORKDIR /srv/advisor

COPY package.json package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund && \
    mkdir -p /codex-auth /sandbox && \
    chown -R node:node /srv/advisor /codex-auth /sandbox

COPY --chown=node:node server.mjs audit.mjs schema-validate.mjs \
     advisor-schema.json classification-schema.json audit-schema.json audit-input-schema.json ./

USER node
EXPOSE 8081

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD node -e "fetch('http://127.0.0.1:8081/health').then(r=>{if(!r.ok)process.exit(1)}).catch(()=>process.exit(1))"

CMD ["node", "server.mjs"]
