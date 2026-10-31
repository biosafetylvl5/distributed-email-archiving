import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { expect, it } from "vitest";
import { originPattern } from "../src/config";

it("declares the optional host permissions requested by runtime options in both manifests", () => {
  for (const browser of ["chrome", "firefox"]) {
    execFileSync(process.execPath, ["build.mjs", browser], { env: { ...process.env, API_URL: "" } });
    const manifest = JSON.parse(readFileSync(`dist/${browser}/manifest.json`, "utf8"));
    expect(manifest.optional_host_permissions).toContain(originPattern("http://localhost:8787"));
    expect(manifest.optional_host_permissions).toContain("https://*/*");
    expect(manifest.host_permissions).toEqual([]);
    expect(manifest.version).toBe("0.3.0");
  }
});
