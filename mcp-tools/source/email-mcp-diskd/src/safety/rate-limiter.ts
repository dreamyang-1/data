/**
 * Token-bucket rate limiter.
 *
 * Limits the number of send operations per account within a time window.
 * Defaults to 10 sends per 60-second window.
 */

export default class RateLimiter {
  private buckets = new Map<string, { tokens: number; lastRefill: number }>();

  private readonly maxTokens: number;

  private readonly windowMs: number;

  constructor(maxPerMinute = 10) {
    this.maxTokens = maxPerMinute;
    this.windowMs = 60_000;
  }

  /**
   * Try to consume a token for the given account.
   * @returns `true` if allowed, `false` if rate-limited.
   */
  tryConsume(accountName: string): boolean {
    const now = Date.now();
    let bucket = this.buckets.get(accountName);

    if (!bucket) {
      bucket = { tokens: this.maxTokens, lastRefill: now };
      this.buckets.set(accountName, bucket);
    }

    // Refill tokens based on elapsed time
    const elapsed = now - bucket.lastRefill;
    const refill = Math.floor((elapsed / this.windowMs) * this.maxTokens);
    if (refill > 0) {
      bucket.tokens = Math.min(this.maxTokens, bucket.tokens + refill);
      bucket.lastRefill = now;
    }

    if (bucket.tokens > 0) {
      bucket.tokens -= 1;
      return true;
    }

    return false;
  }

  /**
   * Get remaining tokens for an account.
   */
  remaining(accountName: string): number {
    const bucket = this.buckets.get(accountName);
    if (!bucket) return this.maxTokens;

    // Calculate refill
    const elapsed = Date.now() - bucket.lastRefill;
    const refill = Math.floor((elapsed / this.windowMs) * this.maxTokens);
    return Math.min(this.maxTokens, bucket.tokens + refill);
  }
}
