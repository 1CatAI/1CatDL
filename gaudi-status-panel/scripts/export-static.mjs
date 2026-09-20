import { cp, mkdir, rm, stat, writeFile } from "node:fs/promises";
import path from "node:path";

const root = process.cwd();
const sourceUrl = process.env.GAUDI_PANEL_EXPORT_URL ?? "http://127.0.0.1:3100/";
const clientRoot = path.join(root, "dist", "client");
const outputRoot = path.join(root, "deploy", "public");

async function isFile(filePath) {
  try {
    return (await stat(filePath)).isFile();
  } catch {
    return false;
  }
}

async function fetchPage(url) {
  const response = await fetch(url, {
    headers: { Accept: "text/html" },
    signal: AbortSignal.timeout(10_000),
  });
  if (!response.ok) throw new Error(`Production page returned HTTP ${response.status} for ${url}`);
  const html = await response.text();
  if (!html.includes("Gaudi2") && !html.includes("单卡 GPU 实例")) {
    throw new Error(`Production HTML is missing the expected product surface for ${url}`);
  }
  if (/https?:\/\/(?:127\.0\.0\.1|localhost):\d+/i.test(html)) {
    throw new Error(`Production HTML contains a loopback URL for ${url}`);
  }
  return html;
}

const pages = [
  { path: "index.html", url: new URL("/", sourceUrl).toString() },
  { path: "rental/index.html", url: new URL("/rental", sourceUrl).toString() },
];
const htmlByPath = new Map();
for (const page of pages) htmlByPath.set(page.path, await fetchPage(page.url));

await rm(outputRoot, { recursive: true, force: true });
await mkdir(outputRoot, { recursive: true });
await cp(path.join(clientRoot, "_next"), path.join(outputRoot, "_next"), {
  recursive: true,
});

for (const name of ["favicon.svg"]) {
  const source = path.join(clientRoot, name);
  if (await isFile(source)) {
    await cp(source, path.join(outputRoot, name));
  }
}

await cp(path.join(clientRoot, "brand"), path.join(outputRoot, "brand"), { recursive: true });

for (const [relativePath, html] of htmlByPath) {
  const target = path.join(outputRoot, relativePath);
  await mkdir(path.dirname(target), { recursive: true });
  await writeFile(target, html, "utf8");
}

const referencedPaths = new Set(
  [...htmlByPath.values()].flatMap((html) =>
    [...html.matchAll(/(?:src|href)=["'](\/(?!\/)[^"'#?]*)/g)].map((match) => decodeURIComponent(match[1])),
  ),
);
for (const referencedPath of referencedPaths) {
  if (referencedPath === "/" || referencedPath.startsWith("/api/")) continue;
  const localPath = path.join(outputRoot, referencedPath.replace(/^\/+/, ""));
  if (!(await isFile(localPath))) {
    throw new Error(`Missing referenced static asset: ${referencedPath}`);
  }
}

const exportedHtml = [...htmlByPath.values()].join("\n");
console.log(
  JSON.stringify({
    sourceUrl,
    outputRoot,
    htmlBytes: Buffer.byteLength(exportedHtml),
    pages: pages.map((page) => page.path),
    referencedAssets: referencedPaths.size,
  }),
);
