# Bundled browser dependencies

These files ship with Yoke and run without npm or a CDN. Keep this inventory
current when replacing an upstream distribution.

| Package | Version | Distribution files |
| --- | --- | --- |
| DOMPurify | 3.4.17 | `purify.es.mjs` |
| Preact | 10.27.3 | `preact.module.js`, `hooks.module.js` |
| HTM | 3.1.1 | `htm.module.js` |
| Marked | 16.2.1 | `marked.esm.js` |
| KaTeX | 0.18.7 | `katex/katex.mjs`, CSS, and fonts |

Download replacements from the package's versioned npm tarball. Verify its
`dist.integrity` digest from the npm registry before copying distribution files,
and retain the upstream license. Preact's renderer and hooks must come from the
same release.

Audit these versions against the OSV npm database as well as running the Python
and TypeScript lockfile audits. `npm audit` in `clients/typescript` does not
include these bundled files. DOMPurify 3.4.17 fixes sanitizer bypasses affecting
the previous 3.2.6 copy; Preact 10.27.3 fixes CVE-2026-22028 in 10.27.2.

After an update, run the Markdown/math regressions and browser scripts listed
in the repository's development instructions.
