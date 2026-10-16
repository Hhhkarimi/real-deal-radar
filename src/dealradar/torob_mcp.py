"""Bounded JSON-RPC client for the pinned Torob MCP stdio server."""
import json
import os
import pathlib
import re
import selectors
import subprocess
import time


class MCPError(RuntimeError):
    def __init__(self, code, http_status=None):
        self.code, self.http_status = code, http_status
        super().__init__(code)


class TorobMCP:
    def __init__(self, config):
        entry = pathlib.Path(os.environ.get('TOROB_MCP_ENTRY', str(config['_base'] / 'vendor/torob-mcp/dist/stdio.js')))
        if not entry.is_file():
            raise MCPError('MCP_BUILD_MISSING')
        self.process = subprocess.Popen(['node', str(entry)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, bufsize=0,
                                        env={k:v for k,v in os.environ.items() if not any(s in k.upper() for s in ['TOKEN','SECRET','FEED_URL','CHAT_ID'])})
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        self.buffer, self.identifier = b'', 0
        try:
            self.rpc('initialize', dict(protocolVersion='2025-03-26', capabilities={},
                     clientInfo=dict(name='real-deal-radar', version='0.7.0')), timeout=15)
            self.write(dict(jsonrpc='2.0', method='notifications/initialized'))
        except Exception:
            self.close()
            raise

    def write(self, message):
        self.process.stdin.write((json.dumps(message)+'\n').encode())
        self.process.stdin.flush()

    def rpc(self, method, params, timeout=60):
        self.identifier += 1
        identifier = self.identifier
        self.write(dict(jsonrpc='2.0', id=identifier, method=method, params=params))
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            while b'\n' in self.buffer:
                line, self.buffer = self.buffer.split(b'\n', 1)
                if not line.strip(): continue
                message = json.loads(line)
                if message.get('id') != identifier: continue
                if 'error' in message: raise MCPError('MCP_PROTOCOL_ERROR')
                return message['result']
            if not self.selector.select(max(0, deadline-time.monotonic())): break
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk: raise MCPError('MCP_PROCESS_EXITED')
            self.buffer += chunk
            if len(self.buffer) > 8_000_000: raise MCPError('MCP_RESPONSE_TOO_LARGE')
        raise MCPError('MCP_TIMEOUT')

    def call(self, name, arguments):
        result = self.rpc('tools/call', dict(name=name, arguments=arguments))
        payload = result.get('structuredContent')
        if payload is None:
            payload = json.loads(next(c['text'] for c in result.get('content', []) if c.get('type') == 'text'))
        if result.get('isError') or payload.get('error'):
            error = payload.get('error', {})
            status = re.search(r'HTTP (\d{3})', error.get('message',''))
            raise MCPError(error.get('code', 'MCP_TOOL_ERROR'), error.get('http_status') or (int(status[1]) if status else None))
        return payload

    def close(self):
        self.selector.close()
        self.process.stdin.close()
        self.process.terminate()
        try: self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.kill(); self.process.wait()
        self.process.stdout.close()

    def __enter__(self): return self
    def __exit__(self, *args): self.close()
