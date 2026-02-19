# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in PostForge, please report it
privately using [GitHub Private Vulnerability Reporting](https://github.com/AndyCappDev/postforge/security/advisories/new)
rather than opening a public issue.

I will acknowledge reports within 48 hours and aim to provide a fix or
mitigation plan within 7 days.

## What Counts as a Security Issue

PostForge interprets PostScript files, which are programs. The PostScript
language includes file I/O and system operators that are intentionally
implemented. However, the following would be considered security issues:

- **Sandbox escapes** — Executing host system commands or accessing resources
  outside the interpreter's intended scope
- **Path traversal** — Accessing files outside expected directories through
  crafted file paths
- **Memory safety** — Crashes or memory corruption that could be exploited
  through crafted PostScript input
- **Denial of service** — Inputs that cause unbounded resource consumption
  beyond what the PostScript program requests (e.g., an interpreter bug that
  causes infinite loops or memory leaks independent of the PS program logic)

## What Is Not a Security Issue

- PostScript programs that consume large amounts of memory or CPU by design
  (e.g., deeply recursive procedures) — this is normal PostScript behavior
- The `file`, `deletefile`, `renamefile`, or `run` operators working as
  specified in the PostScript Language Reference Manual

## Supported Versions

Security fixes are applied to the latest release on the `master` branch.
