# Changelog

All notable changes to this project will be documented in this file.

The format follows [Conventional Commits](https://www.conventionalcommits.org/) and is generated with [cocogitto](https://docs.cocogitto.io/).

<!-- next-header -->

## Unreleased ([3886bac..4e6910c](https://github.com/codefuturist/email-mcp/compare/3886bac..4e6910c))

#### ✨ Features

- **(alerts)** add notification setup diagnostics and AI-configurable alerts - ([34e288a](https://github.com/codefuturist/email-mcp/commit/34e288acb3a3330fd4eca0a583540ec877d9912c))
- **(alerts)** add urgency-based multi-channel notification system - ([b2425df](https://github.com/codefuturist/email-mcp/commit/b2425df6c917436e056e5cc8002ce684fc898694))
- **(cli)** add notify command for testing desktop notifications - ([687f7d2](https://github.com/codefuturist/email-mcp/commit/687f7d26449d97d56bd9d94b7e67f3b798b8e13e))
- **(cli)** add interactive MCP client installation command - ([e2369c7](https://github.com/codefuturist/email-mcp/commit/e2369c7f03df1e506b0bb11e0e5c471a0313ec6b))
- **(cli)** add interactive account CRUD and config edit commands - ([aaa8af5](https://github.com/codefuturist/email-mcp/commit/aaa8af501e7738cf049bd0b4a29ee74f0dbee3bb))
- **(hooks)** add customizable presets and static rule matching - ([138c08e](https://github.com/codefuturist/email-mcp/commit/138c08e0708f49e795e036e2245022fe060a0950))
- **(watcher)** add IMAP IDLE monitoring with AI triage - ([5ed0388](https://github.com/codefuturist/email-mcp/commit/5ed0388ccb0a781220407b3800723bf8191eb2f9))
- add AI-optimised email tools and context improvements - ([d7b01a4](https://github.com/codefuturist/email-mcp/commit/d7b01a48e57491d68ac605583ede6c1a92b2b70d))
- add provider-aware label management (ProtonMail/Gmail/IMAP keywords) - ([85609e5](https://github.com/codefuturist/email-mcp/commit/85609e5f181ea3c01ef26b4fe27a69bafb549141))
- add IMAP move/delete reliability and find_email_folder tool - ([3886bac](https://github.com/codefuturist/email-mcp/commit/3886bacc83eb8b4200f16695468e9029ade32c40))

#### 🐛 Bug Fixes

- **(cli)** add TTY guard and fix IMAP STARTTLS display - ([d9bca69](https://github.com/codefuturist/email-mcp/commit/d9bca695af07e311ec249379827c175e8dac483b))
- virtual folder detection and find_email_folder reliability - ([3c44c22](https://github.com/codefuturist/email-mcp/commit/3c44c226e7b3bf2666479e4d5761c8777d8c5e9c))

#### 📚 Documentation

- update tool count to 42 in README - ([4e6910c](https://github.com/codefuturist/email-mcp/commit/4e6910c04a8dd46efc739079c8e6aa613a7edfaf))
- add pnpm install and usage instructions - ([13c8d4b](https://github.com/codefuturist/email-mcp/commit/13c8d4bf3006fa4fb5f014eb630006a478082a23))

- - -
## [v0.3.1](https://github.com/codefuturist/email-mcp/compare/18c2821a9c29cb266d2f0263c0868e261f044b03..v0.3.1) - 2026-03-24
#### 🐛 Bug Fixes
- (**ci**) use pnpm for release workflow - ([81fed59](https://github.com/codefuturist/email-mcp/commit/81fed5981b7cb183fd39ca4181918f2e8b3b140d)) - nickgorodilov
- exit with code 78 (EX_CONFIG) when configuration is missing - ([9883c02](https://github.com/codefuturist/email-mcp/commit/9883c0288f3a3819833f11afa0764399d345e844)) - nickgorodilov
#### 📚 Documentation
- update README for diskd-ai fork - ([18c2821](https://github.com/codefuturist/email-mcp/commit/18c2821a9c29cb266d2f0263c0868e261f044b03)) - nickgorodilov

- - -

## [v0.2.1](https://github.com/codefuturist/email-mcp/compare/bd6f94d6f0d1f7f4beca5aa8061f2892a40f0ce0..v0.2.1) - 2026-02-20
#### 🐛 Bug Fixes
- (**labels**) fix critical parameter swap and multiple label bugs - ([bd6f94d](https://github.com/codefuturist/email-mcp/commit/bd6f94d6f0d1f7f4beca5aa8061f2892a40f0ce0)) - Colin
- defer post-connect work until MCP handshake completes - ([7847da0](https://github.com/codefuturist/email-mcp/commit/7847da07b4241e73282b2a36a9dd1a362dfb8656)) - Colin
#### Tests
- (**integration**) expand plain connection tests to match STARTTLS and SSL coverage - ([8bd3d77](https://github.com/codefuturist/email-mcp/commit/8bd3d7752ca18037ca899899a1e14688b961c0b1)) - Colin
- (**integration**) add connection mode tests for plain, STARTTLS, and implicit SSL - ([ccbefb7](https://github.com/codefuturist/email-mcp/commit/ccbefb78248f0f08d31c5b227347f286f350c9f9)) - Colin
- (**integration**) add integration test suite with GreenMail and Testcontainers - ([1cc72fe](https://github.com/codefuturist/email-mcp/commit/1cc72fec8166842fa92ad8c7957c2ec28df327ac)) - Colin
#### Build
- (**docker**) add OCI manifest annotations for GHCR multi-arch images - ([2aeb938](https://github.com/codefuturist/email-mcp/commit/2aeb93857e95d99b2cf4435e4eee7cd7a47aecdc)) - Colin
- (**docker**) add docker and goreleaser scripts, fix build for dockers_v2 context - ([56102f4](https://github.com/codefuturist/email-mcp/commit/56102f42ce81bba8c9ab8f442926d1b9704d2ab4)) - Colin
- (**docker**) add GoReleaser dockers_v2 for GHCR and Docker Hub publishing - ([83483a8](https://github.com/codefuturist/email-mcp/commit/83483a8879228b3ec213414f2f7c53e9cce3f497)) - Colin
- (**docker**) add Dockerfile, docker-compose, and CI docker build - ([e9f0a9f](https://github.com/codefuturist/email-mcp/commit/e9f0a9f2179a59de064879456204c8c3b4f3945b)) - Colin
- add lefthook git hooks, report output, upgrade actions and node to v24 - ([8665419](https://github.com/codefuturist/email-mcp/commit/86654197b1a1f252d6c67d8a5fd67f09100f4fd4)) - Colin
#### CI
- (**docker**) enable docker hub publishing - ([f2a8d44](https://github.com/codefuturist/email-mcp/commit/f2a8d44fb8e503a0ef053a716e00b5814625daf8)) - Colin
- refactor workflows to use codefuturist/shared-workflows@v1 - ([815292c](https://github.com/codefuturist/email-mcp/commit/815292c91e6215592cd3172a91600cf42b2224e0)) - Colin
- add docker-sha workflow, workflow_dispatch, action upgrades and lint fixes - ([ddfbcdc](https://github.com/codefuturist/email-mcp/commit/ddfbcdc27af83175c3fec3c666ebd1f23d0631f4)) - Colin
- improve Docker tag strategy - ([99785a0](https://github.com/codefuturist/email-mcp/commit/99785a0ea5c01579046783c3fdf7347932e77fdb)) - Colin
- add weekly Docker rebuild workflow for base image updates - ([08f77f9](https://github.com/codefuturist/email-mcp/commit/08f77f9d06c8fd5b6de65a08c9ff89b556e7f2c0)) - Colin
#### Chores
- (**eslint**) exclude integration tests from eslint - ([e3bcc12](https://github.com/codefuturist/email-mcp/commit/e3bcc122bb9d71bfdfd77040d4419b96296a162d)) - Colin
- (**gitignore**) update .gitignore to include comprehensive rules for various environments and tools - ([4c55dea](https://github.com/codefuturist/email-mcp/commit/4c55dea709e592f3e9f8b01d449846742774c07f)) - Colin
- fix changelog separator for cocogitto - ([55510c3](https://github.com/codefuturist/email-mcp/commit/55510c34bb44ac377e91e1c628d7a810ed2e6d6e)) - Colin

- - -


## [v0.1.0](https://github.com/codefuturist/email-mcp/releases/tag/v0.1.0) — Initial Release

First public release of email-mcp.

#### ✨ Features

- Full IMAP + SMTP email server for MCP clients
- 42 tools, 7 prompts, 6 resources
- Multi-account support with XDG-compliant TOML config
- Guided interactive setup wizard with provider auto-detection
- Gmail, Outlook, Yahoo, iCloud, Fastmail, ProtonMail, Zoho, GMX support
- OAuth2 XOAUTH2 for Gmail and Microsoft 365 _(experimental)_
- Email scheduling with OS-level scheduler integration
- Real-time IMAP IDLE watcher with AI-powered triage
- Urgency-based desktop / webhook alerts
- Provider-aware label management
- ICS/iCalendar extraction from emails
- Email analytics (volume, top senders, daily trends)
- Token-bucket rate limiter and audit trail
- MCP client auto-installer (Claude Desktop, VS Code, Cursor, Windsurf)
