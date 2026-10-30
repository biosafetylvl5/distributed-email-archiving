// the WebExtension API: Firefox's promise-based `browser`, else Chrome's
// `chrome` (promise-based too in Manifest V3)
const g = globalThis as { browser?: typeof chrome; chrome?: typeof chrome };
export const ext = (g.browser ?? g.chrome) as typeof chrome;
