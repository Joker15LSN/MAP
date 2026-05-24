FROM node:22-bookworm-slim

WORKDIR /app

COPY package.json package-lock.json* ./
RUN npm config set registry https://registry.npmmirror.com && npm install --no-fund --no-audit

# Keep an offline dependency seed so runtime can restore /app/node_modules
# even when source code is bind-mounted and the server has no internet.
RUN mkdir -p /opt/frontend_node_modules && cp -a /app/node_modules/. /opt/frontend_node_modules/

COPY . .

EXPOSE 5173

CMD ["npm", "run", "dev"]
