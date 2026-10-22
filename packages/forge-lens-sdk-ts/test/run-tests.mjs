// Test discovery for Node >= 18: `node --test "glob"` only expands
// globs on Node >= 21, and shell expansion differs by OS. Importing
// each compiled *.test.js registers its tests with node:test, which
// runs them automatically at process exit — identical on 18/20/22.
import { readdir } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const dir = join(dirname(fileURLToPath(import.meta.url)), "../dist/test");
const files = (await readdir(dir))
  .filter((f) => f.endsWith(".test.js"))
  .sort();
if (files.length === 0) {
  console.error(`no compiled tests found in ${dir} — run npm run build first`);
  process.exit(1);
}
for (const f of files) {
  await import(join(dir, f));
}
