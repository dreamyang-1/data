/**
 * Typed event bus for internal email events.
 *
 * Decouples the IMAP IDLE watcher from MCP notification hooks.
 * Uses Node's built-in EventEmitter with typed event maps.
 */

import { EventEmitter } from 'node:events';
import type { EmailMeta } from '../types/index.js';

// ---------------------------------------------------------------------------
// Event payloads
// ---------------------------------------------------------------------------

export interface NewEmailEvent {
  account: string;
  mailbox: string;
  emails: EmailMeta[];
}

export interface ExpungeEvent {
  account: string;
  mailbox: string;
  count: number;
}

// ---------------------------------------------------------------------------
// Event map
// ---------------------------------------------------------------------------

interface EmailEventMap {
  'email:new': [NewEmailEvent];
  'email:expunge': [ExpungeEvent];
}

// ---------------------------------------------------------------------------
// Typed EventBus
// ---------------------------------------------------------------------------

export class EmailEventBus extends EventEmitter<EmailEventMap> {}

/** Singleton event bus shared across the application. */
const eventBus = new EmailEventBus();
export default eventBus;
