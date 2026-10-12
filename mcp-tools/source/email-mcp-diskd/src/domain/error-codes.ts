/**
 * Error classification for MCP tool responses.
 *
 * Pure domain module -- no I/O, no side effects.
 * Converts raw errors into structured codes that downstream
 * consumers (app-service, web UI) can map to user-friendly messages.
 */

import formatImapError from '../utils/imap-error.js';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type ErrorCode =
  | 'AUTH_FAILED'
  | 'AUTH_OAUTH_EXPIRED'
  | 'CONNECTION_REFUSED'
  | 'CONNECTION_TIMEOUT'
  | 'DNS_FAILED'
  | 'TLS_ERROR'
  | 'ACCOUNT_NOT_FOUND'
  | 'RATE_LIMITED'
  | 'MAILBOX_NOT_FOUND'
  | 'QUOTA_EXCEEDED'
  | 'VALIDATION_ERROR'
  | 'SERVER_ERROR';

export interface ClassifiedError {
  readonly code: ErrorCode;
  readonly message: string;
  readonly protocol?: 'imap' | 'smtp';
  readonly account?: string;
}

export interface ErrorContext {
  readonly tool: string;
  readonly account?: string;
  readonly protocol?: 'imap' | 'smtp';
}

// ---------------------------------------------------------------------------
// Classification rules (ordered, first match wins)
// ---------------------------------------------------------------------------

interface Rule {
  readonly code: ErrorCode;
  readonly matchCode?: readonly string[];
  readonly matchMessage?: RegExp;
  readonly label: string;
}

const RULES: readonly Rule[] = [
  {
    code: 'AUTH_FAILED',
    matchMessage: /Invalid login|Authentication failed|Login failed|AUTH.*FAIL/i,
    label: 'Authentication failed',
  },
  {
    code: 'AUTH_OAUTH_EXPIRED',
    matchMessage: /OAuth2|token expired|refresh_token/i,
    label: 'OAuth2 authorization expired',
  },
  {
    code: 'CONNECTION_REFUSED',
    matchCode: ['ECONNREFUSED'],
    label: 'Connection refused',
  },
  {
    code: 'CONNECTION_TIMEOUT',
    matchCode: ['ETIMEDOUT', 'ESOCKET', 'ECONNRESET'],
    label: 'Connection timed out',
  },
  {
    code: 'DNS_FAILED',
    matchCode: ['ENOTFOUND'],
    label: 'DNS resolution failed',
  },
  {
    code: 'TLS_ERROR',
    matchMessage: /certificate|TLS|SSL/i,
    label: 'TLS/SSL error',
  },
  {
    code: 'ACCOUNT_NOT_FOUND',
    matchMessage: /Account.*not found/i,
    label: 'Account not found',
  },
  {
    code: 'RATE_LIMITED',
    matchMessage: /rate.limit/i,
    label: 'Rate limit exceeded',
  },
  {
    code: 'MAILBOX_NOT_FOUND',
    matchMessage: /Mailbox.*not found|NONEXISTENT.*Mailbox/i,
    label: 'Mailbox not found',
  },
  {
    code: 'QUOTA_EXCEEDED',
    matchMessage: /OVERQUOTA|quota/i,
    label: 'Quota exceeded',
  },
  {
    code: 'VALIDATION_ERROR',
    matchMessage: /exceeds maximum length|must not be empty|must not contain/i,
    label: 'Validation error',
  },
];

// ---------------------------------------------------------------------------
// Classification
// ---------------------------------------------------------------------------

function extractMessage(err: unknown): string {
  if (err instanceof Error) return formatImapError(err);
  if (typeof err === 'string') return err;
  return String(err ?? 'Unknown error');
}

function extractErrorCode(err: unknown): string | undefined {
  if (err && typeof err === 'object' && 'code' in err) {
    const { code } = err as { code: unknown };
    return typeof code === 'string' ? code : undefined;
  }
  return undefined;
}

function buildMessage(label: string, rawMessage: string, account: string | undefined): string {
  const accountSuffix = account ? ` for account '${account}'` : '';
  return `${label}${accountSuffix}: ${rawMessage}`;
}

export function classifyError(err: unknown, context: ErrorContext): ClassifiedError {
  const rawMessage = extractMessage(err);
  const errorCode = extractErrorCode(err);

  const matched = RULES.find((rule) => {
    const codeMatch = rule.matchCode && errorCode && rule.matchCode.includes(errorCode);
    const messageMatch = rule.matchMessage?.test(rawMessage);
    return codeMatch ?? messageMatch;
  });

  if (matched) {
    return {
      code: matched.code,
      message: buildMessage(matched.label, rawMessage, context.account),
      protocol: context.protocol,
      account: context.account,
    };
  }

  return {
    code: 'SERVER_ERROR',
    message: buildMessage('Unexpected error', rawMessage, context.account),
    protocol: context.protocol,
    account: context.account,
  };
}

/**
 * Build an MCP tool error response with structured JSON content.
 * Used by all tool handlers in their catch blocks.
 */
export function toolErrorResponse(
  err: unknown,
  context: ErrorContext,
): { isError: true; content: [{ type: 'text'; text: string }] } {
  const classified = classifyError(err, context);
  return {
    isError: true,
    content: [{ type: 'text' as const, text: JSON.stringify(classified) }],
  };
}
