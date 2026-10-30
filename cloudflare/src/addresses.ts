// a user's archive addresses

import type { Env } from "./env";

export function archiveDomain(env: Env): string {
  return (env.ARCHIVE_DOMAIN ?? "").trim().toLowerCase();
}

// "+" is the usual form; "__" is for sign-up forms that refuse "+"
export function addressesFor(env: Env, user: string, tag: string): { plus: string; underscore: string } {
  const domain = archiveDomain(env);
  return { plus: `${user}+${tag}@${domain}`, underscore: `${user}__${tag}@${domain}` };
}
