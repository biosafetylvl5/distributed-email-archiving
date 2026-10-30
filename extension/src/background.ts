// the context menu on form fields: "Use archive address for this site" looks
// up (or adds) this site's subscription and types its address in

import { Api } from "./api";
import { loadConfig } from "./config";
import { ext } from "./ext";
import { addressOf, fillInto, findOrCreate, pageInfo } from "./flow";
import { isWebPage } from "./suggest";

const MENU = "use-archive-address";

ext.runtime.onInstalled.addListener(() => {
  ext.contextMenus.create({ id: MENU, title: "Use archive address for this site", contexts: ["editable"] });
});

function tell(tabId: number, message: string): void {
  // the user just clicked, so a plain alert in the page is the clearest answer
  ext.scripting
    .executeScript({ target: { tabId }, func: (m: string) => alert(m), args: [message] })
    .catch(() => undefined);
}

ext.contextMenus.onClicked.addListener(async (info, tab) => {
  if (info.menuItemId !== MENU || tab?.id === undefined || !isWebPage(tab.url)) return;
  const tabId = tab.id;
  try {
    const config = await loadConfig();
    const api = new Api(config);
    const page = await pageInfo(tab);
    const { sub, created } = await findOrCreate(api, page);
    const address = addressOf(sub, config.form);
    const filled = await fillInto(tabId, info.frameId, address);
    if (!filled) tell(tabId, `Couldn't find the email field. Your archive address here is ${address}`);
    else if (created && sub.status !== "approved") {
      tell(tabId, `Added ${sub.tag}, but it's ${sub.status} until the archive's admin approves it.`);
    }
  } catch (err) {
    tell(tabId, `Archive address: ${err instanceof Error ? err.message : String(err)}`);
  }
});
