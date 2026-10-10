import { mcpLog } from '../logging.js';
import type { AccountConfig, WatcherConfig } from '../types/index.js';
import type OAuthService from './oauth.service.js';
import WatcherService from './watcher.service.js';

// Track ImapFlow constructor args to verify auth
const imapFlowInstances: { auth: unknown }[] = [];

// Track event listeners and fetch calls for exists/fetch tests
const eventListeners: Record<string, ((...args: unknown[]) => void)[]> = {};
let fetchSpy: ReturnType<typeof vi.fn> | null = null;

// Mock imapflow module
vi.mock('imapflow', () => {
  class MockImapFlow {
    usable = true;
    mailbox = { uidNext: 100 };
    connect = vi.fn().mockResolvedValue(undefined);
    logout = vi.fn().mockResolvedValue(undefined);
    getMailboxLock = vi.fn().mockResolvedValue({ release: vi.fn() });
    fetch = vi.fn().mockImplementation(async function* () {
      // default: yield nothing
    });
    on = vi.fn().mockImplementation((event: string, cb: (...args: unknown[]) => void) => {
      if (!eventListeners[event]) eventListeners[event] = [];
      eventListeners[event].push(cb);
    });
    constructor(opts: { auth: unknown }) {
      imapFlowInstances.push({ auth: opts.auth });
      fetchSpy = this.fetch;
      // Reset listeners per instance
      for (const key of Object.keys(eventListeners)) delete eventListeners[key];
    }
  }
  return { ImapFlow: MockImapFlow };
});

// Mock logging to prevent side effects
vi.mock('../logging.js', () => ({
  mcpLog: vi.fn().mockResolvedValue(undefined),
}));

// Mock event bus
vi.mock('./event-bus.js', () => ({
  default: { emit: vi.fn() },
}));

const testAccount: AccountConfig = {
  name: 'test',
  email: 'test@example.com',
  username: 'test@example.com',
  password: 'password',
  imap: { host: 'imap.example.com', port: 993, tls: true, starttls: false, verifySsl: true },
  smtp: { host: 'smtp.example.com', port: 465, tls: true, starttls: false, verifySsl: true },
};

const oauthAccount: AccountConfig = {
  name: 'google__GD',
  email: 'user@gmail.com',
  username: 'user@gmail.com',
  imap: { host: 'imap.gmail.com', port: 993, tls: true, starttls: false, verifySsl: false },
  smtp: { host: 'smtp.gmail.com', port: 587, tls: false, starttls: true, verifySsl: false },
  oauth2: {
    provider: 'google',
    clientId: 'test-client-id',
    clientSecret: 'test-client-secret',
    refreshToken: 'test-refresh-token',
  },
};

const noCredentialsAccount: AccountConfig = {
  name: 'broken',
  email: 'broken@example.com',
  username: 'broken@example.com',
  imap: { host: 'imap.example.com', port: 993, tls: true, starttls: false, verifySsl: true },
  smtp: { host: 'smtp.example.com', port: 465, tls: true, starttls: false, verifySsl: true },
};

const enabledConfig: WatcherConfig = { enabled: true, folders: ['INBOX'], idleTimeout: 1740 };

function makeMockOAuthService(accessToken = 'fresh-access-token'): OAuthService {
  return {
    getAccessToken: vi.fn().mockResolvedValue(accessToken),
  } as unknown as OAuthService;
}

describe('WatcherService', () => {
  beforeEach(() => {
    imapFlowInstances.length = 0;
    vi.clearAllMocks();
  });

  it('does not start when disabled', async () => {
    const config: WatcherConfig = { enabled: false, folders: ['INBOX'], idleTimeout: 1740 };
    const watcher = new WatcherService(config, [testAccount]);
    await watcher.start();
    expect(watcher.getStatus()).toHaveLength(0);
  });

  it('returns status after start', async () => {
    const watcher = new WatcherService(enabledConfig, [testAccount]);
    await watcher.start();
    const status = watcher.getStatus();
    expect(status).toHaveLength(1);
    expect(status[0].account).toBe('test');
    expect(status[0].folder).toBe('INBOX');
    expect(status[0].connected).toBe(true);
    await watcher.stop();
  });

  it('stops all connections', async () => {
    const watcher = new WatcherService(enabledConfig, [testAccount]);
    await watcher.start();
    await watcher.stop();
    expect(watcher.getStatus()).toHaveLength(0);
  });

  it('starts idle for multiple folders', async () => {
    const config: WatcherConfig = { enabled: true, folders: ['INBOX', 'Sent'], idleTimeout: 1740 };
    const watcher = new WatcherService(config, [testAccount]);
    await watcher.start();
    const status = watcher.getStatus();
    expect(status).toHaveLength(2);
    await watcher.stop();
  });
});

// ---------------------------------------------------------------------------
// OAuth watcher support
// ---------------------------------------------------------------------------

describe('WatcherService OAuth support', () => {
  beforeEach(() => {
    imapFlowInstances.length = 0;
    vi.clearAllMocks();
  });

  /* REQUIREMENT WATCH-01: OAuth watcher uses OAuthService for token */
  it('uses OAuthService.getAccessToken for OAuth accounts', async () => {
    const mockOAuth = makeMockOAuthService('fresh-token-123');
    const watcher = new WatcherService(enabledConfig, [oauthAccount], mockOAuth);
    await watcher.start();

    // Verify OAuthService was called with account's oauth2 config
    expect(mockOAuth.getAccessToken).toHaveBeenCalledWith(oauthAccount.oauth2);

    // Verify ImapFlow received accessToken auth, not password
    expect(imapFlowInstances).toHaveLength(1);
    const auth = imapFlowInstances[0].auth as { user: string; accessToken?: string; pass?: string };
    expect(auth.accessToken).toBe('fresh-token-123');
    expect(auth.pass).toBeUndefined();
    expect(auth.user).toBe('user@gmail.com');

    await watcher.stop();
  });

  /* REQUIREMENT WATCH-02: Password watcher unchanged */
  it('uses password auth for non-OAuth accounts', async () => {
    const mockOAuth = makeMockOAuthService();
    const watcher = new WatcherService(enabledConfig, [testAccount], mockOAuth);
    await watcher.start();

    // OAuthService should NOT be called for password accounts
    expect(mockOAuth.getAccessToken).not.toHaveBeenCalled();

    // Verify ImapFlow received password auth
    expect(imapFlowInstances).toHaveLength(1);
    const auth = imapFlowInstances[0].auth as { user: string; pass?: string; accessToken?: string };
    expect(auth.pass).toBe('password');
    expect(auth.accessToken).toBeUndefined();

    await watcher.stop();
  });

  /* REQUIREMENT WATCH-03: Skip accounts without credentials */
  it('skips accounts without password or oauth2 config', async () => {
    const watcher = new WatcherService(enabledConfig, [noCredentialsAccount]);
    await watcher.start();

    // No ImapFlow connection should be created
    expect(imapFlowInstances).toHaveLength(0);

    // Warning should be logged
    expect(mcpLog).toHaveBeenCalledWith(
      'warning',
      'watcher',
      expect.stringContaining('No credentials'),
    );

    await watcher.stop();
  });

  /* REQUIREMENT WATCH-04: OAuth token refresh failure skips account */
  it('skips account when OAuth token refresh fails', async () => {
    const mockOAuth = {
      getAccessToken: vi.fn().mockRejectedValue(new Error('Token refresh failed: 401')),
    } as unknown as OAuthService;

    const watcher = new WatcherService(enabledConfig, [oauthAccount], mockOAuth);
    await watcher.start();

    // No ImapFlow connection should be created (token refresh failed)
    expect(imapFlowInstances).toHaveLength(0);

    // Warning should be logged
    expect(mcpLog).toHaveBeenCalledWith(
      'warning',
      'watcher',
      expect.stringContaining('OAuth token refresh failed'),
    );

    await watcher.stop();
  });

  /* REQUIREMENT WATCH-05: Mixed accounts -- OAuth fails, password succeeds */
  it('continues with password account when OAuth account fails', async () => {
    const mockOAuth = {
      getAccessToken: vi.fn().mockRejectedValue(new Error('Token refresh failed')),
    } as unknown as OAuthService;

    const watcher = new WatcherService(enabledConfig, [oauthAccount, testAccount], mockOAuth);
    await watcher.start();

    // Only the password account should have connected
    expect(imapFlowInstances).toHaveLength(1);
    const auth = imapFlowInstances[0].auth as { pass?: string };
    expect(auth.pass).toBe('password');

    const status = watcher.getStatus();
    // OAuth account should still be in status (started but not connected)
    // Password account should be connected
    const connectedStatuses = status.filter((s) => s.connected);
    expect(connectedStatuses).toHaveLength(1);
    expect(connectedStatuses[0].account).toBe('test');

    await watcher.stop();
  });
});

// ---------------------------------------------------------------------------
// FETCH uid mode
// ---------------------------------------------------------------------------

describe('WatcherService FETCH uid mode', () => {
  beforeEach(() => {
    imapFlowInstances.length = 0;
    fetchSpy = null;
    for (const key of Object.keys(eventListeners)) delete eventListeners[key];
    vi.clearAllMocks();
  });

  /* REQUIREMENT WATCH-06: handleNewEmails must use UID FETCH, not sequence FETCH.
     ImapFlow fetch(range, query, options) uses options.uid (3rd arg) to decide
     between UID FETCH and FETCH. Passing uid:true only in query (2nd arg) adds
     UID as a data item but still sends plain FETCH with sequence numbers. When
     lastSeenUid is larger than the message count, plain FETCH fails with
     "The specified message set is invalid." */
  it('calls ImapFlow fetch with uid option in 3rd argument', async () => {
    const watcher = new WatcherService(enabledConfig, [testAccount]);
    await watcher.start();

    // Simulate exists event (new message arrived)
    const existsHandlers = eventListeners.exists ?? [];
    expect(existsHandlers.length).toBeGreaterThan(0);
    existsHandlers[0]({ path: 'INBOX', count: 2, prevCount: 1 });

    // Give handleNewEmails a tick to execute
    await new Promise<void>((resolve) => {
      setTimeout(resolve, 50);
    });

    expect(fetchSpy).toHaveBeenCalledTimes(1);
    const [range, , options] = fetchSpy!.mock.calls[0];

    // Range should be UID-based (lastSeenUid + 1 = 100, from uidNext=100)
    expect(range).toBe('100:*');

    // 3rd argument must have uid: true for UID FETCH
    expect(options).toBeDefined();
    expect(options).toHaveProperty('uid', true);

    await watcher.stop();
  });
});
