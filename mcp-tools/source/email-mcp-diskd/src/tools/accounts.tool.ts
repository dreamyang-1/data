/**
 * MCP tool: list_accounts
 */

import type { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';

import type ConnectionManager from '../connections/manager.js';

export default function registerAccountsTools(
  server: McpServer,
  connections: ConnectionManager,
): void {
  server.tool(
    'list_accounts',
    'List all configured email accounts. Call this first to discover available account names for use with other tools.',
    {},
    { readOnlyHint: true, destructiveHint: false },
    async () => {
      const names = connections.getAccountNames();
      const accounts = names.map((name) => {
        const cfg = connections.getAccount(name);
        return {
          name: cfg.name,
          email: cfg.email,
          fullName: cfg.fullName ?? null,
        };
      });

      return {
        content: [
          {
            type: 'text' as const,
            text:
              accounts.length > 0
                ? accounts
                    .map((a) => `• ${a.name}: ${a.email}${a.fullName ? ` (${a.fullName})` : ''}`)
                    .join('\n')
                : 'No accounts configured.',
          },
        ],
      };
    },
  );
}
