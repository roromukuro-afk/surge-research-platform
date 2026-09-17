import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));

/** @type {import('next').NextConfig} */
const nextConfig = {
  // Pin the trace root to this app. Without it Next walks up looking for a
  // lockfile and can settle on one outside the repository entirely, which makes
  // the build depend on what happens to be in the developer's home directory.
  outputFileTracingRoot: here,
};

export default nextConfig;
