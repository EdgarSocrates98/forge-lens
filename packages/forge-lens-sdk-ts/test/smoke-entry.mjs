// Smoke: resolve the package through its own "exports" map — the exact
// path consumers take (Node self-reference). If "main"/"exports" point
// at a file tsc doesn't emit, this fails to resolve instead of passing
// silently because tests import ../src/ relatively.
import {
  LensTracer,
  HTTPExporter,
  InMemoryExporter,
  LensClient,
  LensQueryError,
} from "@forge-lens/sdk";

if (
  typeof LensTracer !== "function" ||
  typeof HTTPExporter !== "function" ||
  typeof InMemoryExporter !== "function" ||
  typeof LensClient !== "function" ||
  typeof LensQueryError !== "function"
) {
  console.error("package entry resolved but exports are missing");
  process.exit(1);
}
const tracer = new LensTracer({ serviceName: "smoke", shutdownHook: false });
tracer.span("s", () => {});
console.log("package entry OK");
