/**
 * MCP Server factory.
 *
 * Creates and configures the McpServer instance with capabilities.
 */

import { createRequire } from 'node:module';

import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';

const esmRequire = createRequire(import.meta.url);
const pkg = esmRequire('../package.json') as { version: string };

export const PKG_NAME = 'email-mcp';
export const PKG_VERSION = pkg.version;

export default function createServer(): McpServer {
  return new McpServer(
    {
      name: PKG_NAME,
      version: PKG_VERSION,
    },
    {
      capabilities: {
        tools: { listChanged: true },
        prompts: { listChanged: true },
        resources: { subscribe: true, listChanged: true },
        logging: {},
      },
    },
  );
}
