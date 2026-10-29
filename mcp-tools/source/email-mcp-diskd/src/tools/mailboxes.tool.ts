/**
 * MCP tool: list_mailboxes
 */

import type { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { z } from 'zod';
import { toolErrorResponse } from '../domain/error-codes.js';
import type ImapService from '../services/imap.service.js';

export default function registerMailboxesTools(server: McpServer, imapService: ImapService): void {
  server.tool(
    'list_mailboxes',
    'List all mailbox folders for an account with unread counts and special-use flags. Use list_accounts first to get the account name.',
    {
      account: z.string().describe('Account name from list_accounts'),
    },
    { readOnlyHint: true, destructiveHint: false },
    async ({ account }) => {
      try {
        const mailboxes = await imapService.listMailboxes(account);

        const lines = mailboxes.map((mb) => {
          const badge = mb.unseenMessages > 0 ? ` (${mb.unseenMessages} unread)` : '';
          const special = mb.specialUse ? ` [${mb.specialUse}]` : '';
          return `• ${mb.path}${special} — ${mb.totalMessages} messages${badge}`;
        });

        return {
          content: [
            {
              type: 'text' as const,
              text: lines.join('\n') || 'No mailboxes found.',
            },
          ],
        };
      } catch (err) {
        return toolErrorResponse(err, { tool: 'list_mailboxes', account, protocol: 'imap' });
      }
    },
  );
}
