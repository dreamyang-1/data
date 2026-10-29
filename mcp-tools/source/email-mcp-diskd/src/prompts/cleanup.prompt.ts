/**
 * MCP Prompt: cleanup_inbox
 *
 * Instructs the LLM to analyze and clean up the inbox with
 * categorization and optional execution.
 */

import type { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { z } from 'zod';

export default function registerCleanupPrompt(server: McpServer): void {
  server.prompt(
    'cleanup_inbox',
    'AI-guided inbox cleanup — categorizes emails and suggests or executes organization actions.',
    {
      account: z.string().describe('Account name to clean up'),
      older_than_days: z
        .string()
        .default('30')
        .describe('Only consider emails older than N days (default: 30)'),
      dry_run: z
        .string()
        .default('true')
        .describe("'true' = suggest only, 'false' = execute actions (default: true)"),
    },
    async ({ account, older_than_days: olderThan, dry_run: dryRun }) => {
      const days = Math.max(parseInt(olderThan, 10) || 30, 1);
      const execute = dryRun === 'false';
      const beforeDate = new Date(Date.now() - days * 86400000).toISOString().split('T')[0];

      return {
        messages: [
          {
            role: 'user' as const,
            content: {
              type: 'text' as const,
              text: `Help me clean up the "${account}" inbox. Analyze emails older than ${days} days.

Follow these steps:
1. Call list_emails with account="${account}", mailbox="INBOX", pageSize=50, before="${beforeDate}" to scan old emails.
2. For emails needing more context, use get_email to read the body.
3. Categorize each email into one of these groups:

🗑️ **Delete candidates** — Old read promotional emails, automated notifications, expired offers
📁 **Archive candidates** — Old read conversational emails with no pending action
🚩 **Needs attention** — Unread or flagged emails that may still need a response
✅ **Keep** — Recent or important emails that belong in inbox

4. Look for newsletter patterns (List-Unsubscribe headers, recurring senders with promotional content).

Present your findings as:

## 🧹 Inbox Cleanup — ${account}
**Scanned:** [count] emails older than ${days} days

### 🗑️ Delete ([count])
- [Subject] from [Sender] — [Reason]

### 📁 Archive ([count])
- [Subject] from [Sender] — [Reason]

### 🚩 Needs Attention ([count])
- [Subject] from [Sender] — [Why this needs action]

### ✅ Keep ([count])
- [Subject] from [Sender] — [Why to keep]

### 📰 Newsletter Sources
- [Sender] — [Frequency estimate] — Consider unsubscribing?

### Summary
- Total scanned: X
- Suggested deletions: X
- Suggested archives: X
- Estimated space savings: X

${
  execute
    ? `**Mode: EXECUTE** — After presenting the plan, proceed to execute the cleanup using bulk_action to move/delete emails as categorized. Report results for each action.`
    : `**Mode: DRY RUN** — Present the cleanup plan only. Ask if I want to proceed with any of the suggested actions.`
}`,
            },
          },
        ],
      };
    },
  );
}
