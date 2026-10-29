/**
 * MCP Prompt: triage_inbox
 *
 * Instructs the LLM to analyze unread emails and categorize them by
 * urgency and action needed.
 */

import type { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { z } from 'zod';

export default function registerTriagePrompt(server: McpServer): void {
  server.prompt(
    'triage_inbox',
    'Analyze unread emails and categorize by urgency. Produces a structured triage report with recommended actions.',
    {
      account: z.string().describe('Account name to triage'),
      mailbox: z.string().default('INBOX').describe('Mailbox to triage (default: INBOX)'),
      limit: z
        .string()
        .default('20')
        .describe('Maximum number of unread emails to analyze (default: 20)'),
    },
    async ({ account, mailbox, limit }) => {
      const limitNum = Math.min(Math.max(parseInt(limit, 10) || 20, 1), 50);
      return {
        messages: [
          {
            role: 'user' as const,
            content: {
              type: 'text' as const,
              text: `Triage the unread emails in the "${mailbox}" mailbox of the "${account}" account.

Follow these steps:
1. Call list_emails with account="${account}", mailbox="${mailbox}", seen=false, pageSize=${limitNum} to get unread emails.
2. For each email, read the subject, sender, and preview to classify it into ONE of these categories:
   🔴 **Urgent** — Requires immediate attention (time-sensitive, from important contacts, contains deadlines)
   🟡 **Needs Response** — Requires a reply but not time-critical
   🔵 **FYI** — Informational, no action needed (newsletters, notifications, CC'd emails)
   ⚪ **Promotional** — Marketing, spam, or low-priority automated emails
3. For emails that need more context, use get_email to read the full body.

Output a triage report in this format:

## 📬 Inbox Triage — ${account}
**${mailbox}** | Analyzed: [count] unread emails

### 🔴 Urgent ([count])
- **[Subject]** from [Sender] — [Brief reason why urgent] → [Recommended action]

### 🟡 Needs Response ([count])
- **[Subject]** from [Sender] — [Brief summary] → [Suggested response approach]

### 🔵 FYI ([count])
- **[Subject]** from [Sender] — [One-line summary]

### ⚪ Promotional ([count])
- **[Subject]** from [Sender]

### Recommended Actions
1. [Most important action first]
2. [Second priority]
...

Suggest which emails to mark as read, flag for follow-up, or archive.`,
            },
          },
        ],
      };
    },
  );
}
