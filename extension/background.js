// The whole of the extension's behaviour: press the button, land on the
// portal. Nothing is read from the page, nothing is sent anywhere.
import { readPortalUrl } from "./portal.js";

chrome.action.onClicked.addListener(async () => {
  const { url } = await readPortalUrl();
  if (url) {
    chrome.tabs.create({ url });
    return;
  }
  // Nothing configured yet: opening a blank tab would look broken, so send
  // the user to the one place that can fix it.
  chrome.runtime.openOptionsPage();
});

chrome.runtime.onInstalled.addListener(async (details) => {
  if (details.reason !== "install") return;
  const { url } = await readPortalUrl();
  // Installed by policy with the address already set, there is nothing to ask.
  if (!url) chrome.runtime.openOptionsPage();
});
