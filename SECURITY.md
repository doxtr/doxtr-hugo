# Security Policy

## Supported versions

doxtr-hugo is in early development. Security fixes are applied to the latest
released version on the `main` branch.

| Version | Supported          |
| ------- | ------------------ |
| 0.1.x   | :white_check_mark: |

## Reporting a vulnerability

Please report security issues privately rather than opening a public issue.

Use GitHub's private vulnerability reporting
("Report a vulnerability" under the repository's **Security** tab) so the report
reaches the maintainers without public disclosure.

Include:

- a description of the issue and its impact,
- steps to reproduce (a minimal Sphinx project is ideal), and
- the doxtr-hugo and Sphinx versions affected.

We will acknowledge the report, investigate, and coordinate a fix and
disclosure timeline with you. Please give us a reasonable window to release a
fix before any public disclosure.

## Scope note

The builder invokes external diagram/math rendering binaries (`plantuml`,
`dot`, `mmdc`, `d2`) only when they are present on `PATH`. Treat Sphinx source
documents as you would any build input: diagram directives are passed to those
tools, so build untrusted documentation sources in an appropriately sandboxed
environment.
