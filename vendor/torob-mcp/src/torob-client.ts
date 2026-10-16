import * as z from 'zod/v4';
import { TorobError } from './errors.js';

const API_ORIGIN = 'https://api.torob.com';
const ALLOWED_PATHS = new Set([
  '/v4/base-product/search/', '/v4/base-product/details/', '/v4/base-product/sellers/',
  '/v4/base-product/similar-base-product/', '/suggestion2/',
]);
const sleep = (ms: number) => new Promise<void>(resolve => setTimeout(resolve, ms));
export type Provenance = { source: 'torob.com'; currency: 'IRT'; price_unit: 'toman';
  fetched_at: string; cache_expires_at: string; cached: boolean };
type Cached = { data: unknown; fetchedAt: number; expiresAt: number; bytes: number };
type Options = { fetcher?: typeof fetch; intervalMs?: number; timeoutMs?: number;
  ttlMs?: number; maxEntries?: number; maxResponseBytes?: number; maxCacheBytes?: number };

/** Bounded per-process cache and serial upstream gate; no accounts or user cookies. */
export class TorobClient {
  private readonly fetcher: typeof fetch;
  private readonly intervalMs: number;
  private readonly timeoutMs: number;
  private readonly ttlMs: number;
  private readonly maxEntries: number;
  private readonly maxResponseBytes: number;
  private readonly maxCacheBytes: number;
  private cacheBytes = 0;
  private cache = new Map<string, Cached>();
  private pending = new Map<string, Promise<Cached>>();
  private tail: Promise<unknown> = Promise.resolve();
  private nextStart = 0;
  private blockedUntil = 0;
  constructor(options: Options = {}) {
    this.fetcher = options.fetcher ?? globalThis.fetch;
    this.intervalMs = options.intervalMs ?? 500;
    this.timeoutMs = options.timeoutMs ?? 12000;
    this.ttlMs = options.ttlMs ?? 60000;
    this.maxEntries = options.maxEntries ?? 100;
    this.maxResponseBytes = options.maxResponseBytes ?? 5_000_000;
    this.maxCacheBytes = options.maxCacheBytes ?? 8_000_000;
  }

  async get<S extends z.ZodType>(path: string, params: Record<string, string | number | undefined>, schema: S) {
    const url = new URL(path, API_ORIGIN);
    if (!ALLOWED_PATHS.has(url.pathname) || url.origin !== API_ORIGIN || url.username || url.password || url.hash) {
      throw new TorobError('INVALID_UPSTREAM_URL', 'This upstream path is not permitted.');
    }
    for (const [key, value] of Object.entries(params)) if (value !== undefined) url.searchParams.set(key, String(value));
    url.searchParams.sort();
    const key = url.href;
    let entry = this.cache.get(key);
    const cached = entry !== undefined && entry.expiresAt > Date.now();
    if (!cached) {
      let promise = this.pending.get(key);
      if (!promise) {
        if (this.pending.size >= 32) throw new TorobError('BUSY', 'Too many upstream requests are queued. Retry shortly.', 5);
        promise = this.tail.then(() => this.load(url));
        this.tail = promise.catch(() => undefined);
        this.pending.set(key, promise);
      }
      try { entry = await promise; } finally { if (this.pending.get(key) === promise) this.pending.delete(key); }
    }
    const result = schema.safeParse(entry!.data);
    if (!result.success) {
      this.removeCached(key);
      throw new TorobError('UPSTREAM_SCHEMA_CHANGED', 'Torob returned an unexpected data structure. Update the adapter.');
    }
    const provenance: Provenance = { source: 'torob.com', currency: 'IRT', price_unit: 'toman',
      fetched_at: new Date(entry!.fetchedAt).toISOString(),
      cache_expires_at: new Date(entry!.expiresAt).toISOString(), cached };
    return { data: result.data, provenance };
  }

  private async load(url: URL): Promise<Cached> {
    for (let attempt = 0; attempt < 2; attempt++) {
      if (this.blockedUntil > Date.now()) {
        throw new TorobError('UPSTREAM_BLOCKED', 'Torob asked this server to wait before another request.',
          Math.ceil((this.blockedUntil - Date.now()) / 1000));
      }
      await sleep(Math.max(0, this.nextStart - Date.now()));
      this.nextStart = Date.now() + this.intervalMs;
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), this.timeoutMs);
      try {
        const response = await this.fetcher(url, {
          method: 'GET', redirect: 'error', signal: controller.signal,
          headers: { accept: 'application/json', 'user-agent': 'torob-mcp/0.1.0 (read-only)',
            origin: 'https://torob.com', referer: 'https://torob.com/' },
        });
        if ([401, 403, 429].includes(response.status)) {
          const retryAfter = response.headers.get('retry-after');
          const seconds = retryAfter && /^\d+$/.test(retryAfter) ? Number(retryAfter) :
            retryAfter ? Math.ceil((Date.parse(retryAfter) - Date.now()) / 1000) : 30;
          const wait = Number.isFinite(seconds) ? Math.max(1, Math.min(seconds, 3600)) : 30;
          this.blockedUntil = Date.now() + wait * 1000;
          await response.body?.cancel();
          throw new TorobError('UPSTREAM_BLOCKED', 'Torob rate-limited or blocked the request. Retry after the indicated delay.', wait, response.status);
        }
        if (!response.ok) {
          await response.body?.cancel();
          if (response.status >= 500 && attempt === 0) { await sleep(800); continue; }
          throw new TorobError(response.status === 404 ? 'NOT_FOUND' : 'UPSTREAM_HTTP_ERROR',
            response.status === 404 ? 'This Torob resource was not found.' : `Torob returned HTTP ${response.status}.`, undefined, response.status);
        }
        const contentType = response.headers.get('content-type') ?? '';
        if (!contentType.includes('json')) {
          await response.body?.cancel();
          throw new TorobError('UPSTREAM_NON_JSON', 'Torob returned a non-JSON response, possibly an access challenge.');
        }
        const body = response.body?.getReader();
        if (!body) throw new TorobError('UPSTREAM_INVALID_JSON', 'Torob returned an empty response.');
        const decoder = new TextDecoder();
        let length = 0, content = '';
        while (true) {
          const { done, value } = await body.read();
          if (done) break;
          length += value.byteLength;
          if (length > this.maxResponseBytes) {
            await body.cancel();
            throw new TorobError('UPSTREAM_RESPONSE_TOO_LARGE', 'Torob response exceeded the allowed size.');
          }
          content += decoder.decode(value, { stream: true });
        }
        content += decoder.decode();
        let data: unknown;
        try { data = JSON.parse(content); }
        catch { throw new TorobError('UPSTREAM_INVALID_JSON', 'Torob returned invalid JSON.'); }
        const now = Date.now();
        const entry = { data, fetchedAt: now, expiresAt: now + this.ttlMs, bytes: length };
        this.removeCached(url.href);
        while (this.cache.size && (this.cache.size >= this.maxEntries || this.cacheBytes + length > this.maxCacheBytes)) {
          this.removeCached(this.cache.keys().next().value!);
        }
        if (this.maxEntries > 0 && length <= this.maxCacheBytes) {
          this.cache.set(url.href, entry);
          this.cacheBytes += length;
        }
        return entry;
      } catch (error) {
        if (error instanceof TorobError) throw error;
        if (attempt === 0 && !controller.signal.aborted) { await sleep(800); continue; }
        throw new TorobError(controller.signal.aborted ? 'UPSTREAM_TIMEOUT' : 'UPSTREAM_NETWORK_ERROR',
          controller.signal.aborted ? 'Torob did not respond within the timeout.' : 'Could not connect to Torob. Check network access from this server.');
      } finally { clearTimeout(timer); }
    }
    throw new TorobError('UPSTREAM_NETWORK_ERROR', 'Could not connect to Torob.');
  }

  private removeCached(key: string) {
    const existing = this.cache.get(key);
    if (existing) { this.cacheBytes -= existing.bytes; this.cache.delete(key); }
  }
}
