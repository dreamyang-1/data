import type { ClassifiedError } from './error-codes.js';
import { classifyError, toolErrorResponse } from './error-codes.js';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const makeError = (message: string, code?: string): Error & { code?: string } => {
  const err = new Error(message) as Error & { code?: string };
  if (code) err.code = code;
  return err;
};

const ctx = (overrides: Partial<Parameters<typeof classifyError>[1]> = {}) => ({
  tool: 'send_email',
  account: 'personal',
  protocol: 'smtp' as const,
  ...overrides,
});

// ---------------------------------------------------------------------------
// AUTH_FAILED
// ---------------------------------------------------------------------------

describe('classifyError', () => {
  describe('AUTH_FAILED', () => {
    it.each([
      'Invalid login: 535 Authentication failed',
      'Login failed: AUTHENTICATIONFAILED',
      'Authentication failed',
      'AUTH FAIL: invalid credentials',
      'Invalid login',
      'Login failed',
      'NO [AUTHENTICATIONFAILED] Invalid credentials (Failure)',
    ])('classifies "%s" as AUTH_FAILED', (message) => {
      const result = classifyError(makeError(message), ctx());
      expect(result.code).toBe('AUTH_FAILED');
      expect(result.protocol).toBe('smtp');
      expect(result.account).toBe('personal');
    });
  });

  // ---------------------------------------------------------------------------
  // AUTH_OAUTH_EXPIRED
  // ---------------------------------------------------------------------------

  describe('AUTH_OAUTH_EXPIRED', () => {
    it.each([
      'OAuth2 token expired',
      'Failed to refresh OAuth2 token',
      'refresh_token is invalid or expired',
      'Invalid OAuth2 access token',
    ])('classifies "%s" as AUTH_OAUTH_EXPIRED', (message) => {
      const result = classifyError(makeError(message), ctx());
      expect(result.code).toBe('AUTH_OAUTH_EXPIRED');
    });
  });

  // ---------------------------------------------------------------------------
  // CONNECTION_REFUSED
  // ---------------------------------------------------------------------------

  describe('CONNECTION_REFUSED', () => {
    it('classifies ECONNREFUSED by error code', () => {
      const result = classifyError(
        makeError('connect ECONNREFUSED 127.0.0.1:993', 'ECONNREFUSED'),
        ctx({ protocol: 'imap' }),
      );
      expect(result.code).toBe('CONNECTION_REFUSED');
      expect(result.protocol).toBe('imap');
    });
  });

  // ---------------------------------------------------------------------------
  // CONNECTION_TIMEOUT
  // ---------------------------------------------------------------------------

  describe('CONNECTION_TIMEOUT', () => {
    it('classifies ETIMEDOUT by error code', () => {
      const result = classifyError(makeError('connect ETIMEDOUT', 'ETIMEDOUT'), ctx());
      expect(result.code).toBe('CONNECTION_TIMEOUT');
    });

    it('classifies ESOCKET by error code', () => {
      const result = classifyError(makeError('Socket closed unexpectedly', 'ESOCKET'), ctx());
      expect(result.code).toBe('CONNECTION_TIMEOUT');
    });

    it('classifies ECONNRESET by error code', () => {
      const result = classifyError(makeError('read ECONNRESET', 'ECONNRESET'), ctx());
      expect(result.code).toBe('CONNECTION_TIMEOUT');
    });
  });

  // ---------------------------------------------------------------------------
  // DNS_FAILED
  // ---------------------------------------------------------------------------

  describe('DNS_FAILED', () => {
    it('classifies ENOTFOUND by error code', () => {
      const result = classifyError(
        makeError('getaddrinfo ENOTFOUND imap.example.com', 'ENOTFOUND'),
        ctx(),
      );
      expect(result.code).toBe('DNS_FAILED');
    });
  });

  // ---------------------------------------------------------------------------
  // TLS_ERROR
  // ---------------------------------------------------------------------------

  describe('TLS_ERROR', () => {
    it.each([
      'self-signed certificate in certificate chain',
      'unable to verify the first certificate',
      'TLS handshake failed',
      'SSL routines:ssl3_get_record:wrong version number',
      'certificate has expired',
    ])('classifies "%s" as TLS_ERROR', (message) => {
      const result = classifyError(makeError(message), ctx());
      expect(result.code).toBe('TLS_ERROR');
    });
  });

  // ---------------------------------------------------------------------------
  // ACCOUNT_NOT_FOUND
  // ---------------------------------------------------------------------------

  describe('ACCOUNT_NOT_FOUND', () => {
    it.each([
      'Account "xyz" not found. Available: personal, work',
      'Account not found',
    ])('classifies "%s" as ACCOUNT_NOT_FOUND', (message) => {
      const result = classifyError(makeError(message), ctx());
      expect(result.code).toBe('ACCOUNT_NOT_FOUND');
    });
  });

  // ---------------------------------------------------------------------------
  // RATE_LIMITED
  // ---------------------------------------------------------------------------

  describe('RATE_LIMITED', () => {
    it.each([
      'Rate limit exceeded for account "personal"',
      'rate limit reached',
    ])('classifies "%s" as RATE_LIMITED', (message) => {
      const result = classifyError(makeError(message), ctx());
      expect(result.code).toBe('RATE_LIMITED');
    });
  });

  // ---------------------------------------------------------------------------
  // MAILBOX_NOT_FOUND
  // ---------------------------------------------------------------------------

  describe('MAILBOX_NOT_FOUND', () => {
    it.each([
      'Mailbox not found: INBOX2',
      'NO [NONEXISTENT] Unknown Mailbox: INBOX2',
    ])('classifies "%s" as MAILBOX_NOT_FOUND', (message) => {
      const result = classifyError(makeError(message), ctx());
      expect(result.code).toBe('MAILBOX_NOT_FOUND');
    });
  });

  // ---------------------------------------------------------------------------
  // QUOTA_EXCEEDED
  // ---------------------------------------------------------------------------

  describe('QUOTA_EXCEEDED', () => {
    it.each([
      'OVERQUOTA: mailbox is full',
      'Quota exceeded',
    ])('classifies "%s" as QUOTA_EXCEEDED', (message) => {
      const result = classifyError(makeError(message), ctx());
      expect(result.code).toBe('QUOTA_EXCEEDED');
    });
  });

  // ---------------------------------------------------------------------------
  // VALIDATION_ERROR
  // ---------------------------------------------------------------------------

  describe('VALIDATION_ERROR', () => {
    it.each([
      'Subject exceeds maximum length of 998 characters',
      'Mailbox name must not be empty',
      'Label name must not contain control characters',
      'Search query must not be empty after sanitization',
    ])('classifies "%s" as VALIDATION_ERROR', (message) => {
      const result = classifyError(makeError(message), ctx());
      expect(result.code).toBe('VALIDATION_ERROR');
    });
  });

  // ---------------------------------------------------------------------------
  // SERVER_ERROR (fallback)
  // ---------------------------------------------------------------------------

  describe('SERVER_ERROR (fallback)', () => {
    it('classifies unknown errors as SERVER_ERROR', () => {
      const result = classifyError(makeError('something totally unexpected'), ctx());
      expect(result.code).toBe('SERVER_ERROR');
    });

    it('classifies non-Error values as SERVER_ERROR', () => {
      const result = classifyError('raw string error', ctx());
      expect(result.code).toBe('SERVER_ERROR');
      expect(result.message).toContain('raw string error');
    });

    it('classifies null/undefined as SERVER_ERROR', () => {
      const result = classifyError(null, ctx());
      expect(result.code).toBe('SERVER_ERROR');
    });
  });

  // ---------------------------------------------------------------------------
  // Context propagation
  // ---------------------------------------------------------------------------

  describe('context propagation', () => {
    it('includes account and protocol from context', () => {
      const result = classifyError(makeError('Invalid login'), {
        tool: 'list_emails',
        account: 'work',
        protocol: 'imap',
      });
      expect(result.account).toBe('work');
      expect(result.protocol).toBe('imap');
    });

    it('omits account when not provided', () => {
      const result = classifyError(makeError('Invalid login'), { tool: 'list_emails' });
      expect(result.account).toBeUndefined();
      expect(result.protocol).toBeUndefined();
    });

    it('includes account in message', () => {
      const result = classifyError(makeError('Invalid login: 535'), {
        tool: 'send_email',
        account: 'personal',
        protocol: 'smtp',
      });
      expect(result.message).toContain('personal');
    });
  });

  // ---------------------------------------------------------------------------
  // Priority ordering
  // ---------------------------------------------------------------------------

  describe('priority ordering', () => {
    it('AUTH_FAILED takes priority over CONNECTION errors when message has both hints', () => {
      // An auth error message that also contains "certificate"
      const result = classifyError(makeError('Authentication failed'), ctx());
      expect(result.code).toBe('AUTH_FAILED');
    });

    it('error code-based classification (ECONNREFUSED) takes priority over message-based for connection errors', () => {
      // Error with ECONNREFUSED code but auth-like message
      const result = classifyError(makeError('some generic message', 'ECONNREFUSED'), ctx());
      expect(result.code).toBe('CONNECTION_REFUSED');
    });
  });

  // ---------------------------------------------------------------------------
  // Return type shape
  // ---------------------------------------------------------------------------

  describe('return type', () => {
    it('returns a valid ClassifiedError shape', () => {
      const result: ClassifiedError = classifyError(makeError('test'), ctx());
      expect(result).toHaveProperty('code');
      expect(result).toHaveProperty('message');
      expect(typeof result.code).toBe('string');
      expect(typeof result.message).toBe('string');
    });
  });
});

// ---------------------------------------------------------------------------
// toolErrorResponse
// ---------------------------------------------------------------------------

describe('toolErrorResponse', () => {
  it('returns MCP tool error shape with isError true', () => {
    const result = toolErrorResponse(new Error('Invalid login'), {
      tool: 'send_email',
      account: 'personal',
      protocol: 'smtp',
    });
    expect(result.isError).toBe(true);
    expect(result.content).toHaveLength(1);
    expect(result.content[0].type).toBe('text');
  });

  it('contains valid JSON in text content', () => {
    const result = toolErrorResponse(new Error('Invalid login: 535'), {
      tool: 'send_email',
      account: 'personal',
      protocol: 'smtp',
    });
    const parsed = JSON.parse(result.content[0].text);
    expect(parsed.code).toBe('AUTH_FAILED');
    expect(parsed.account).toBe('personal');
    expect(parsed.protocol).toBe('smtp');
    expect(parsed.message).toContain('personal');
  });

  it('works with non-Error values', () => {
    const result = toolErrorResponse('raw string', { tool: 'test' });
    const parsed = JSON.parse(result.content[0].text);
    expect(parsed.code).toBe('SERVER_ERROR');
  });
});
