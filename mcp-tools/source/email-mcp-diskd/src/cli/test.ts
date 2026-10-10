/**
 * Connection test command.
 *
 * Tests IMAP and SMTP connections for all or a specific account.
 */

import { intro, log, outro, spinner as p_spinner } from '@clack/prompts';

import { loadConfig } from '../config/loader.js';
import ConnectionManager from '../connections/manager.js';

import type { AccountConfig } from '../types/index.js';

async function testAccount(account: AccountConfig): Promise<boolean> {
  log.step(`Testing account: ${account.name} (${account.email})`);
  let ok = true;

  // Test IMAP
  const spinner = p_spinner();
  spinner.start(`IMAP ${account.imap.host}:${account.imap.port}...`);
  const imapResult = await ConnectionManager.testImap(account);
  if (imapResult.success) {
    spinner.stop(
      `IMAP  ✓ ${account.imap.host}:${account.imap.port} — ${imapResult.details?.messages} messages, ${imapResult.details?.folders} folders`,
    );
  } else {
    spinner.stop(`IMAP  ✗ ${account.imap.host}:${account.imap.port} — ${imapResult.error}`);
    ok = false;
  }

  // Test SMTP
  spinner.start(`SMTP ${account.smtp.host}:${account.smtp.port}...`);
  const smtpResult = await ConnectionManager.testSmtp(account);
  if (smtpResult.success) {
    spinner.stop(`SMTP  ✓ ${account.smtp.host}:${account.smtp.port} — authenticated`);
  } else {
    spinner.stop(`SMTP  ✗ ${account.smtp.host}:${account.smtp.port} — ${smtpResult.error}`);
    ok = false;
  }

  return ok;
}

export default async function runTest(accountFilter?: string): Promise<void> {
  intro('email-mcp test');

  const config = await loadConfig();

  const accounts = accountFilter
    ? config.accounts.filter((a) => a.name === accountFilter)
    : config.accounts;

  if (accounts.length === 0) {
    if (accountFilter) {
      log.error(
        `Account "${accountFilter}" not found. Available: ${config.accounts.map((a) => a.name).join(', ')}`,
      );
    } else {
      log.error('No accounts configured.');
    }
    throw new Error('No matching accounts found');
  }

  let allPassed = true;

  // Sequential testing — connections can't be parallelized reliably
  // eslint-disable-next-line no-restricted-syntax
  for (const account of accounts) {
    // eslint-disable-next-line no-await-in-loop
    const ok = await testAccount(account);
    if (!ok) allPassed = false;
  }

  if (allPassed) {
    outro('All accounts OK ✅');
  } else {
    outro('Some connections failed ❌');
    throw new Error('Connection tests failed');
  }
}
