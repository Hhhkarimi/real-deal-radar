import { serveStdio, StdioServerTransport } from '@modelcontextprotocol/server/stdio';
import { createServer } from './server.js';
import { TorobService } from './service.js';
const service = new TorobService();
const handle = serveStdio(() => createServer(service), {
  transport: new StdioServerTransport(process.stdin, process.stdout, { maxBufferSize: 64 * 1024 }),
});
process.on('SIGINT', () => { void handle.close(); });
process.on('SIGTERM', () => { void handle.close(); });
