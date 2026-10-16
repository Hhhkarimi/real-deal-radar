import { createEndpoint } from './endpoint.js';
import { TorobService } from './service.js';

type Env = { MCP_TOKEN?: string };
const service = new TorobService();
let current: { token: string; endpoint: ReturnType<typeof createEndpoint> } | undefined;
export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    if (!env.MCP_TOKEN || env.MCP_TOKEN.length < 24) {
      return Response.json({ error: 'Configure the MCP_TOKEN Worker secret (at least 24 characters).' }, { status: 503 });
    }
    if (!current || current.token !== env.MCP_TOKEN) {
      if (current) await current.endpoint.close();
      current = { token: env.MCP_TOKEN, endpoint: createEndpoint({ service, token: env.MCP_TOKEN }) };
    }
    return current.endpoint.fetch(request);
  },
};
