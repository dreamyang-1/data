declare module 'vitest' {
  export interface ProvidedContext {
    greenmailHost: string;
    greenmailSmtpPort: number;
    greenmailSmtpsPort: number;
    greenmailImapPort: number;
    greenmailImapsPort: number;
  }
}
