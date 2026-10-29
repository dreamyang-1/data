/**
 * MCP tools: list_labels, add_label, remove_label, create_label, delete_label
 *
 * Provider-aware label management. Automatically detects whether the account
 * uses ProtonMail (folder-based labels), Gmail (X-GM-LABELS), or standard
 * IMAP keywords, and applies the correct strategy.
 */

import type { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { z } from 'zod';
import { classifyError, toolErrorResponse } from '../domain/error-codes.js';
import audit from '../safety/audit.js';
import { validateLabelName } from '../safety/validation.js';

import type ImapService from '../services/imap.service.js';

export default function registerLabelTools(server: McpServer, imapService: ImapService): void {
  // ---------------------------------------------------------------------------
  // list_labels
  // ---------------------------------------------------------------------------
  server.tool(
    'list_labels',
    'List available labels for an email account. ' +
      'Auto-detects the label system: ProtonMail folder-labels, Gmail X-GM-LABELS, or IMAP keywords. ' +
      'ProtonMail note: labels are represented as IMAP folders under the Labels/ prefix. ' +
      'Use list_emails with mailbox="Labels/<name>" to find emails tagged with a ProtonMail label.',
    {
      account: z.string().describe('Account name from list_accounts'),
    },
    { readOnlyHint: true },
    async ({ account }) => {
      try {
        const labels = await imapService.listLabels(account);
        if (labels.length === 0) {
          return {
            content: [
              {
                type: 'text' as const,
                text: 'No labels found. Use create_label to create one.',
              },
            ],
          };
        }

        const { strategy } = labels[0];
        const lines = [
          `🏷️ ${labels.length} label(s) — strategy: ${strategy}`,
          '',
          ...labels.map((l) => `  • ${l.name}${l.path ? ` (${l.path})` : ''}`),
        ];
        return {
          content: [{ type: 'text' as const, text: lines.join('\n') }],
        };
      } catch (err) {
        return toolErrorResponse(err, { tool: 'list_labels', account, protocol: 'imap' });
      }
    },
  );

  // ---------------------------------------------------------------------------
  // add_label
  // ---------------------------------------------------------------------------
  server.tool(
    'add_label',
    'Add a label to an email. ' +
      'For ProtonMail, this copies the email into the corresponding Labels/<name> folder. ' +
      'For Gmail and standard IMAP, this sets a keyword flag on the message.',
    {
      account: z.string().describe('Account name from list_accounts'),
      emailId: z.string().describe('Email ID (UID) from list_emails'),
      mailbox: z.string().describe('Mailbox containing the email (must be a real folder)'),
      label: z.string().describe('Label name to add (e.g., "Important", "Project-X")'),
    },
    { readOnlyHint: false, destructiveHint: false, idempotentHint: true },
    async ({ account, emailId, mailbox, label }) => {
      try {
        const cleanLabel = validateLabelName(label);
        await imapService.addLabel(account, emailId, mailbox, cleanLabel);
        await audit.log('add_label', account, { emailId, mailbox, label: cleanLabel }, 'ok');
        return {
          content: [
            { type: 'text' as const, text: `🏷️ Label "${label}" added to email ${emailId}.` },
          ],
        };
      } catch (err) {
        const classified = classifyError(err, { tool: 'add_label', account, protocol: 'imap' });
        await audit.log(
          'add_label',
          account,
          { emailId, mailbox, label },
          'error',
          classified.message,
        );
        return toolErrorResponse(err, { tool: 'add_label', account, protocol: 'imap' });
      }
    },
  );

  // ---------------------------------------------------------------------------
  // remove_label
  // ---------------------------------------------------------------------------
  server.tool(
    'remove_label',
    'Remove a label from an email. For ProtonMail, this removes the email from the label folder. ' +
      'For Gmail and standard IMAP, this removes a keyword flag.',
    {
      account: z.string().describe('Account name from list_accounts'),
      emailId: z.string().describe('Email ID (UID) from list_emails'),
      mailbox: z.string().describe('Mailbox containing the email (must be a real folder)'),
      label: z.string().describe('Label name to remove'),
    },
    { readOnlyHint: false, destructiveHint: false, idempotentHint: true },
    async ({ account, emailId, mailbox, label }) => {
      try {
        const cleanLabel = validateLabelName(label);
        await imapService.removeLabel(account, emailId, mailbox, cleanLabel);
        await audit.log('remove_label', account, { emailId, mailbox, label: cleanLabel }, 'ok');
        return {
          content: [
            {
              type: 'text' as const,
              text: `🏷️ Label "${label}" removed from email ${emailId}.`,
            },
          ],
        };
      } catch (err) {
        const classified = classifyError(err, { tool: 'remove_label', account, protocol: 'imap' });
        await audit.log(
          'remove_label',
          account,
          { emailId, mailbox, label },
          'error',
          classified.message,
        );
        return toolErrorResponse(err, { tool: 'remove_label', account, protocol: 'imap' });
      }
    },
  );

  // ---------------------------------------------------------------------------
  // create_label
  // ---------------------------------------------------------------------------
  server.tool(
    'create_label',
    'Create a new label. For ProtonMail, creates a folder under Labels/. ' +
      'For standard IMAP keywords, labels are auto-created on first use — this is a no-op.',
    {
      account: z.string().describe('Account name from list_accounts'),
      name: z
        .string()
        .describe(
          'Label name (e.g., "Project-X"). For nested labels use "/" separator (e.g., "Work/Urgent").',
        ),
    },
    { readOnlyHint: false, destructiveHint: false },
    async ({ account, name }) => {
      try {
        const cleanName = validateLabelName(name);
        await imapService.createLabel(account, cleanName);
        await audit.log('create_label', account, { name: cleanName }, 'ok');
        return {
          content: [{ type: 'text' as const, text: `🏷️ Label "${name}" created.` }],
        };
      } catch (err) {
        const classified = classifyError(err, { tool: 'create_label', account, protocol: 'imap' });
        await audit.log('create_label', account, { name }, 'error', classified.message);
        return toolErrorResponse(err, { tool: 'create_label', account, protocol: 'imap' });
      }
    },
  );

  // ---------------------------------------------------------------------------
  // delete_label
  // ---------------------------------------------------------------------------
  server.tool(
    'delete_label',
    'Delete a label. For ProtonMail, deletes the label folder. ' +
      'For standard IMAP keywords, labels cannot be deleted server-wide — use remove_label on individual emails.',
    {
      account: z.string().describe('Account name from list_accounts'),
      name: z.string().describe('Label name to delete'),
    },
    { readOnlyHint: false, destructiveHint: true },
    async ({ account, name }) => {
      try {
        await imapService.deleteLabel(account, name);
        await audit.log('delete_label', account, { name }, 'ok');
        return {
          content: [{ type: 'text' as const, text: `🏷️ Label "${name}" deleted.` }],
        };
      } catch (err) {
        const classified = classifyError(err, { tool: 'delete_label', account, protocol: 'imap' });
        await audit.log('delete_label', account, { name }, 'error', classified.message);
        return toolErrorResponse(err, { tool: 'delete_label', account, protocol: 'imap' });
      }
    },
  );
}
