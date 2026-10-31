# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub's
[security advisories](https://github.com/BenA-SA/linkedin-marketing/security/advisories/new),
not as a public issue. You should get a response within a week.

## Handling credentials

- Keep `LINKEDIN_CLIENT_SECRET`, `LINKEDIN_REFRESH_TOKEN` and `LINKEDIN_ACCESS_TOKEN`
  in a secret manager or the environment. Never commit them or paste them into MCP
  config files.
- The library never logs tokens, never puts them in errors or dry runs, and
  keeps them out of `repr`.
- If a token is ever exposed, revoke it: remove the app's access in your
  LinkedIn settings, or rotate the client secret on the app's Auth tab. Then
  mint a new one.

## Safety features are defence in depth

Read-only mode, allow-lists, caps and dry runs reduce the blast radius of a
mistaken or manipulated agent. They don't replace least-privilege tokens.
Request only the scopes of the modules you actually use (`linkedin-marketing-token
--modules ...`), and give the LinkedIn member the lowest ad-account and page
roles that work.
