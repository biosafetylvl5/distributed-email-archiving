// options: the API address, this browser's token and the address form

import { Api, ApiError } from "./api";
import { loadConfig, originPattern, saveConfig, type AddressForm } from "./config";
import { byId } from "./dom";
import { ext } from "./ext";

function status(message: string, error = false): void {
  const p = byId("status");
  p.textContent = message;
  p.className = error ? "error" : "";
}

async function start(): Promise<void> {
  const config = await loadConfig();
  byId<HTMLInputElement>("api-url").value = config.apiUrl;
  byId<HTMLInputElement>("token").value = config.token;
  for (const r of document.querySelectorAll<HTMLInputElement>('input[name="form"]')) r.checked = r.value === config.form;

  byId<HTMLFormElement>("form").onsubmit = async (e) => {
    e.preventDefault();
    const apiUrl = byId<HTMLInputElement>("api-url").value.trim().replace(/\/+$/, "");
    const token = byId<HTMLInputElement>("token").value.trim();
    const form = (document.querySelector<HTMLInputElement>('input[name="form"]:checked')?.value ?? "plus") as AddressForm;
    const origin = originPattern(apiUrl);
    if (!origin) return status("The API address must start with https://", true);
    // asked for here, on the click: Firefox grants host permissions only on request
    if (!(await ext.permissions.request({ origins: [origin] }))) {
      return status("The extension needs permission to reach the API to work.", true);
    }
    await saveConfig({ apiUrl, token, form });
    try {
      const me = await new Api({ apiUrl, token }).me();
      status(`Saved. Signed in as ${me.user}; addresses end in @${me.archive_domain}.`);
    } catch (err) {
      const why = err instanceof ApiError && err.status === 401 ? "the archive refused the token" : String((err as Error).message);
      status(`Saved, but the check failed: ${why}.`, true);
    }
  };
}

start().catch((err) => status(String(err), true));
