export class TorobError extends Error {
  constructor(
    public readonly code: string,
    message: string,
    public readonly retryAfterSeconds?: number,
    public readonly status?: number,
  ) { super(message); this.name = 'TorobError'; }
}

export function errorPayload(error: unknown) {
  if (error instanceof TorobError) {
    return { error: { code: error.code, message: error.message,
      ...(error.retryAfterSeconds !== undefined ? { retry_after_seconds: error.retryAfterSeconds } : {}) } };
  }
  return { error: { code: 'INTERNAL_ERROR', message: 'The request could not be completed.' } };
}
