# Security Policy

## Supported versions

Only the latest release on `main` receives fixes while the project is pre-1.0.

## Reporting a vulnerability

Please **do not open a public issue**. Use GitHub's
[private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
("Report a vulnerability" under the repository's Security tab). We aim to acknowledge reports
within 5 business days.

## How keys are handled

- `EPOCHAL_API_KEY` and `OPENROUTER_API_KEY` are read from the environment or a local `.env` file,
  which is gitignored.
- Keys are sent only to their own API (`api.epochal.com`, `openrouter.ai`) over HTTPS.
- Keys are never printed, logged, cached, or written to run outputs; `epochal-pilot check` reports only
  whether each key is set. Error messages never include request headers.
- The cache (`.epochal-pilot-cache/`) holds API responses (archived public web pages and model replies), not keys.

If you find a path where a key could leak, please report it as above.
