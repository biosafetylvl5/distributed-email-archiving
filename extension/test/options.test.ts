// @vitest-environment jsdom
import { afterEach, expect, it, vi } from "vitest";

// Import after installing the browser API: options installs its submit handler on load.
afterEach(() => {
  vi.unstubAllGlobals();
  vi.resetModules();
});

it("reports permission-request rejection without saving or leaving an unhandled rejection", async () => {
  const save = vi.fn();
  vi.stubGlobal("chrome", {
    storage: { local: { get: async () => ({ apiUrl: "http://localhost:8787", token: "dea_x" }), set: save } },
    permissions: { request: vi.fn(async () => { throw new Error("Permission unavailable"); }) },
  });
  document.body.innerHTML = '<form id="form"><input id="api-url"><input id="token"><input name="form" value="plus" type="radio" checked></form><p id="status"></p>';
  await import("../src/options");
  const form = document.getElementById("form") as HTMLFormElement;
  await vi.waitFor(() => expect(form.onsubmit).not.toBeNull());
  const result = Promise.resolve(form.onsubmit!.call(form, new Event("submit") as SubmitEvent));
  await expect(result).resolves.toBeUndefined();
  expect(document.getElementById("status")?.textContent).toContain("Permission unavailable");
  expect(document.getElementById("status")?.className).toBe("error");
  expect(save).not.toHaveBeenCalled();
});
