# TypeScript HTTP client

Use Node.js 22.15+ and npm 10+. Run `npm ci && npm run check` to regenerate the
HTTP schema and check types.
Run `npm audit` to check runtime and development dependencies for published
vulnerabilities.

The `openapi-typescript` override selects `@redocly/openapi-core` 2.7.0, which
replaces the vulnerable minimatch dependency used by Redocly 1.x. Overriding
minimatch alone is incompatible with Redocly 1.x's callable CommonJS import.
`generate.mjs` resolves the generator's private `ref-utils.js` import against
that pinned Redocly release and supplies its legacy `styleguide` config alias.
These exceptions run only during schema generation, with Redocly's minimal
validation rules enabled.
Remove the override and wrapper when openapi-typescript supports a patched
Redocly version. Schema regeneration must remain unchanged when updating it.
