import { registerHooks } from "node:module";
import { writeFile } from "node:fs/promises";
import { createConfig } from "@redocly/openapi-core";

// openapi-typescript 7.13 imports this private path, which Redocly 2 no longer
// exports. Keep the exception local to schema generation and the pinned release.
registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier === "@redocly/openapi-core/lib/ref-utils.js") {
      const core = nextResolve("@redocly/openapi-core", context);
      return nextResolve(new URL("./ref-utils.js", core.url).href, context);
    }
    return nextResolve(specifier, context);
  },
});

const { default: openapiTS, astToString, COMMENT_HEADER } = await import("openapi-typescript");
const config = await createConfig({ extends: ["minimal"] });
// Redocly 2 merged the former styleguide object into Config itself.
config.styleguide = config;
const schema = new URL("../../tests/yoke/http/golden/openapi.json", import.meta.url);
const declarations = await openapiTS(schema, { redocly: config });
await writeFile(new URL("./src/schema.d.ts", import.meta.url), COMMENT_HEADER + astToString(declarations));
