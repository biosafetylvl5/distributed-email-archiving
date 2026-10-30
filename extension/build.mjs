// node build.mjs chrome|firefox   -> dist/<browser>/ (unpacked extension)
// node build.mjs package          -> dist/*.zip for the stores, plus the source
//
// API_URL=https://mail-api.example.org node build.mjs …  bakes in the API
// address (as the default, and as a host permission granted at install);
// without it the user enters it in the options and grants access there.

import { execFileSync } from "node:child_process";
import { cpSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { build } from "esbuild";

const pkg = JSON.parse(readFileSync("package.json", "utf8"));
const target = process.argv[2];
const apiUrl = (process.env.API_URL ?? "").replace(/\/+$/, "");

// Firefox needs a fixed id to sign and update; it needn't be a real address
const GECKO_ID = "archive-address@distributed-email-archiving";

function origin(url) {
  const u = new URL(url);
  if (u.protocol !== "https:" && !(u.protocol === "http:" && u.hostname === "localhost")) {
    throw new Error("API_URL must be https (or http://localhost for testing)");
  }
  return `${u.protocol}//${u.hostname}/*`;
}

function manifest(browser) {
  const m = {
    manifest_version: 3,
    name: "Archive address",
    version: pkg.version,
    description: "Add newsletters to your email archive and fill your archive address into their sign-up forms.",
    icons: { 16: "icons/16.png", 32: "icons/32.png", 48: "icons/48.png", 128: "icons/128.png" },
    action: { default_title: "Archive address", default_popup: "popup.html", default_icon: { 16: "icons/16.png", 32: "icons/32.png" } },
    options_ui: { page: "options.html", open_in_tab: true },
    // activeTab + scripting: read or fill the page only when the user clicks
    permissions: ["activeTab", "scripting", "storage", "contextMenus"],
    host_permissions: apiUrl ? [origin(apiUrl)] : [],
    // for an API address entered in the options instead
    optional_host_permissions: ["https://*/*"],
  };
  if (browser === "firefox") {
    m.background = { scripts: ["background.js"] };
    m.browser_specific_settings = {
      gecko: {
        id: GECKO_ID,
        strict_min_version: "140.0",
        // the page's address, title and site name go to the user's own archive
        data_collection_permissions: { required: ["browsingActivity"] },
      },
    };
  } else {
    m.background = { service_worker: "background.js" };
    m.minimum_chrome_version = "121";
  }
  return m;
}

async function buildFor(browser) {
  const out = `dist/${browser}`;
  rmSync(out, { recursive: true, force: true });
  mkdirSync(out, { recursive: true });
  await build({
    entryPoints: ["src/popup.ts", "src/options.ts", "src/background.ts"],
    bundle: true,
    format: "iife",
    target: browser === "firefox" ? "firefox140" : "chrome121",
    outdir: out,
    // unminified, so store reviewers can read what runs
    minify: false,
    legalComments: "none",
    define: { __DEFAULT_API_URL__: JSON.stringify(apiUrl) },
    logLevel: "warning",
  });
  cpSync("static", out, { recursive: true });
  cpSync("icons", `${out}/icons`, { recursive: true });
  writeFileSync(`${out}/manifest.json`, JSON.stringify(manifest(browser), null, 2) + "\n");
  console.log(`built ${out}${apiUrl ? ` for ${new URL(apiUrl).hostname}` : ""}`);
}

function zip(dir, file) {
  rmSync(file, { force: true });
  execFileSync("zip", ["-qrX", `../${file.split("/").pop()}`, "."], { cwd: dir });
  console.log(`packed ${file}`);
}

if (target === "chrome" || target === "firefox") {
  await buildFor(target);
} else if (target === "package") {
  zip("dist/chrome", `dist/archive-address-chrome-${pkg.version}.zip`);
  zip("dist/firefox", `dist/archive-address-firefox-${pkg.version}.zip`);
  // the source, for Mozilla's review of bundled code (from the repo root:
  // this directory without its dependencies, output or credentials, and the
  // shared modules)
  const src = `extension/dist/archive-address-source-${pkg.version}.zip`;
  rmSync(`../${src}`, { force: true });
  execFileSync(
    "zip",
    ["-qrX", src, "extension", "cloudflare/src/tagfields.ts", "cloudflare/src/policy.ts",
     "-x", "extension/node_modules/*", "extension/dist/*", "extension/web-ext-artifacts/*",
     "extension/.amo.env", "extension/.env*"],
    { cwd: ".." },
  ); // prettier-ignore
  console.log(`packed dist/archive-address-source-${pkg.version}.zip`);
} else {
  console.error("usage: node build.mjs chrome|firefox|package");
  process.exit(2);
}
