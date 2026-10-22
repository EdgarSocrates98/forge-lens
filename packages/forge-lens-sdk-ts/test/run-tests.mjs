// Test discovery for Node >= 20 (engines floor): `node --test "glob"`
// only expands globs on Node >= 21, and shell expansion differs by OS.
// Importing each compiled *.test.js registers its tests with node:test,
// which runs them automatically at process exit — identical on 20/22.
// Caveat: all files share one process — unlike `node --test`'s per-file
// isolation, a leak (timers, global state) bleeds across files. Current
// specs are pure; keep it that way or switch to `node --test dist/test`
// once Node >= 21 is the floor.
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
