FROM node:22-slim AS deps
WORKDIR /app
COPY package.json ./
RUN npm install

FROM node:22-slim AS build
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
# NEXT_PUBLIC_* vars are inlined into the client bundle at build time, not
# read at container start, so they're build args here rather than compose
# `environment:`. Defaults match .env.example (the browser hits the
# cloud API's published host port directly, same in dev and compose).
ARG NEXT_PUBLIC_CLOUD_API_URL=http://localhost:8030
ARG NEXT_PUBLIC_DT_ID=DT-0417
ENV NEXT_PUBLIC_CLOUD_API_URL=${NEXT_PUBLIC_CLOUD_API_URL}
ENV NEXT_PUBLIC_DT_ID=${NEXT_PUBLIC_DT_ID}
RUN npm run build

FROM node:22-slim
WORKDIR /app
COPY --from=build /app/.next/standalone ./
COPY --from=build /app/.next/static ./.next/static
COPY --from=build /app/public ./public
EXPOSE 3000
CMD ["node", "server.js"]
