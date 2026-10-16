import { createServer as createHttpServer } from 'node:http';
import { pathToFileURL } from 'node:url';
import { hostHeaderValidation, originValidation, toNodeHandler } from '@modelcontextprotocol/node';
import { createEndpoint } from './endpoint.js';
import type { TorobService } from './service.js';

export function createNodeServer(options: { service?: TorobService; token?: string; allowedHostnames?: string[] } = {}) {
  const endpoint = createEndpoint(options);
  const names = options.allowedHostnames ?? ['localhost', '127.0.0.1', '[::1]'];
  const validateHost = hostHeaderValidation(names);
  const validateOrigin = originValidation(names);
  const handle = toNodeHandler(endpoint, { maxRequestBodySize: 64 * 1024 });
  const server = createHttpServer((request, response) => {
    if (!validateHost(request, response) || !validateOrigin(request, response)) return;
    void handle(request, response).catch(() => {
      if (!response.headersSent) response.writeHead(500, { 'content-type': 'application/json' });
      if (!response.writableEnded) response.end(JSON.stringify({ error: 'Internal server error' }));
    });
  });
  server.requestTimeout = 30000;
  server.headersTimeout = 10000;
  return { server, close: async () => {
    await endpoint.close();
    await new Promise<void>((resolve, reject) => server.close(error => error ? reject(error) : resolve()));
  } };
}

export async function main() {
  const host = process.env.HOST ?? '127.0.0.1';
  const port = Number(process.env.PORT ?? 8787);
  if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('PORT must be an integer from 1 to 65535.');
  const local = ['127.0.0.1', 'localhost', '::1'].includes(host);
  const token = process.env.MCP_TOKEN;
  let names: string[] | undefined;
  if (!local) {
    if (!token || token.length < 24) throw new Error('A non-loopback HOST requires MCP_TOKEN with at least 24 characters.');
    if (!process.env.PUBLIC_URL) throw new Error('A non-loopback HOST requires PUBLIC_URL, e.g. https://mcp.example.com.');
    const publicUrl = new URL(process.env.PUBLIC_URL);
    if (publicUrl.protocol !== 'https:') throw new Error('PUBLIC_URL must use HTTPS; terminate TLS at your reverse proxy.');
    names = [publicUrl.hostname];
  }
  const app = createNodeServer({ token, allowedHostnames: names });
  await new Promise<void>((resolve, reject) => {
    app.server.once('error', reject);
    app.server.listen(port, host, () => { app.server.off('error', reject); resolve(); });
  });
  console.error(`torob-mcp listening on http://${host}:${port}/mcp`);
  const shutdown = () => { void app.close().catch(() => { process.exitCode = 1; }); };
  process.once('SIGINT', shutdown);
  process.once('SIGTERM', shutdown);
}
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch(error => { console.error(error instanceof Error ? error.message : 'Server startup failed.'); process.exitCode = 1; });
}
