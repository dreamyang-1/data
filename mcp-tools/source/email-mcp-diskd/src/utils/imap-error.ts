/**
 * Extract diagnostic details from ImapFlow errors.
 *
 * ImapFlow throws plain `Error` objects whose `.message` is often just
 * "Command failed".  The underlying IMAP response text and error code
 * are available on non-standard properties that we surface here.
 */

interface ImapErrorLike {
  message: string;
  code?: string;
  responseStatus?: string;
  responseText?: string;
  command?: string;
}

function isImapError(err: unknown): err is ImapErrorLike {
  return err instanceof Error;
}

export default function formatImapError(err: unknown): string {
  if (!isImapError(err)) return String(err);

  const parts: string[] = [err.message];

  if (err.code && err.code !== err.message) {
    parts.push(`code=${err.code}`);
  }
  if (err.responseStatus) {
    parts.push(`status=${err.responseStatus}`);
  }
  if (err.responseText) {
    parts.push(`response="${err.responseText}"`);
  }
  if (err.command) {
    parts.push(`cmd=${err.command}`);
  }

  // Fallback: dump all non-standard enumerable keys for unknown error shapes
  if (parts.length === 1) {
    const extra = Object.entries(err as unknown as Record<string, unknown>)
      .filter(([k]) => k !== 'message' && k !== 'stack')
      .map(([k, v]) => `${k}=${JSON.stringify(v)}`)
      .join(', ');
    if (extra) parts.push(extra);
  }

  return parts.join(' | ');
}
