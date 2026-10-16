import { createMcpHandler } from '@modelcontextprotocol/server';
import { createServer, TOOL_NAMES, VERSION } from './server.js';
import { TorobService } from './service.js';

export function createEndpoint(options: { service?: TorobService; token?: string } = {}) {
  const service = options.service ?? new TorobService();
  const handler = createMcpHandler(() => createServer(service), {
    maxRequestBodySize: 64 * 1024,
  });
  return {
    close: () => handler.close(),
    async fetch(request: Request): Promise<Response> {
      const url = new URL(request.url);
      if (url.pathname === '/health' && request.method === 'GET') return Response.json({ status: 'ok', version: VERSION });
      if (url.pathname === '/' && request.method === 'GET') return Response.json({
        name: 'torob-mcp', version: VERSION, endpoint: '/mcp', transport: 'Streamable HTTP',
        read_only: true, tools: TOOL_NAMES, price_unit: 'toman', unofficial: true,
      });
      if (url.pathname !== '/mcp') return Response.json({ error: 'Not found' }, { status: 404 });
      const origin = request.headers.get('origin');
      if (origin && origin !== url.origin) return Response.json({ error: 'Origin not allowed' }, { status: 403 });
      if (options.token && request.headers.get('authorization') !== `Bearer ${options.token}`) {
        return Response.json({ error: 'Unauthorized' }, { status: 401,
          headers: { 'www-authenticate': 'Bearer realm="torob-mcp"' } });
      }
      if (!['GET', 'POST', 'DELETE'].includes(request.method)) {
        return new Response(null, { status: 405, headers: { allow: 'GET, POST, DELETE' } });
      }
      try { return await handler.fetch(request); }
      catch { return Response.json({ error: 'Internal server error' }, { status: 500 }); }
    },
  };
}
