import { inject } from 'vitest';
import type { AccountConfig } from '../../types/index.js';

const TEST_ACCOUNT_NAME = 'integration';
const TEST_EMAIL = 'test@localhost';
const TEST_USER = 'test';

export function getGreenMailPorts() {
  const host = inject('greenmailHost');
  const smtpPort = inject('greenmailSmtpPort');
  const smtpsPort = inject('greenmailSmtpsPort');
  const imapPort = inject('greenmailImapPort');
  const imapsPort = inject('greenmailImapsPort');
  return { host, smtpPort, smtpsPort, imapPort, imapsPort };
}

/** Plain-text connection (no encryption). */
export function buildTestAccount(overrides: Partial<AccountConfig> = {}): AccountConfig {
  const { host, smtpPort, imapPort } = getGreenMailPorts();
  return {
    name: TEST_ACCOUNT_NAME,
    email: TEST_EMAIL,
    fullName: 'Integration Test',
    username: TEST_USER,
    password: TEST_USER,
    imap: {
      host,
      port: imapPort,
      tls: false,
      starttls: false,
      verifySsl: false,
    },
    smtp: {
      host,
      port: smtpPort,
      tls: false,
      starttls: false,
      verifySsl: false,
    },
    ...overrides,
  };
}

/** STARTTLS connection — plain port, upgrades to TLS. */
export function buildStarttlsAccount(): AccountConfig {
  const { host, smtpPort, imapPort } = getGreenMailPorts();
  return {
    name: 'integration-starttls',
    email: TEST_EMAIL,
    fullName: 'STARTTLS Test',
    username: TEST_USER,
    password: TEST_USER,
    imap: {
      host,
      port: imapPort,
      tls: false,
      starttls: true,
      verifySsl: false,
    },
    smtp: {
      host,
      port: smtpPort,
      tls: false,
      starttls: true,
      verifySsl: false,
    },
  };
}

/** Implicit SSL/TLS connection — dedicated TLS ports. */
export function buildSslAccount(): AccountConfig {
  const { host, smtpsPort, imapsPort } = getGreenMailPorts();
  return {
    name: 'integration-ssl',
    email: TEST_EMAIL,
    fullName: 'SSL Test',
    username: TEST_USER,
    password: TEST_USER,
    imap: {
      host,
      port: imapsPort,
      tls: true,
      starttls: false,
      verifySsl: false,
    },
    smtp: {
      host,
      port: smtpsPort,
      tls: true,
      starttls: false,
      verifySsl: false,
    },
  };
}

export function buildSecondTestAccount(): AccountConfig {
  const { host, smtpPort, imapPort } = getGreenMailPorts();
  return {
    name: 'integration-2',
    email: 'bob@localhost',
    fullName: 'Bob Tester',
    username: 'bob',
    password: 'bob',
    imap: {
      host,
      port: imapPort,
      tls: false,
      starttls: false,
      verifySsl: false,
    },
    smtp: {
      host,
      port: smtpPort,
      tls: false,
      starttls: false,
      verifySsl: false,
    },
  };
}

export { TEST_ACCOUNT_NAME, TEST_EMAIL, TEST_USER };
