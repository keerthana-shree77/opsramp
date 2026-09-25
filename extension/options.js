import { SETTING, readPortalUrl, checkPortalUrl } from "./portal.js";

const field = document.getElementById("url");
const message = document.getElementById("message");
const save = document.getElementById("save");
const open = document.getElementById("open");
const managedNote = document.getElementById("managed");

function say(text, tone) {
  message.textContent = text;
  message.className = tone ? `message ${tone}` : "message";
}

const current = await readPortalUrl();
field.value = current.url;
open.disabled = !current.url;

if (current.locked) {
  // Under policy the field is a statement of fact, not a choice.
  field.disabled = true;
  save.disabled = true;
  managedNote.hidden = false;
  document.getElementById("hint").hidden = true;
}

document.getElementById("settings").addEventListener("submit", async (event) => {
  event.preventDefault();
  const checked = checkPortalUrl(field.value);
  if (checked.error) {
    say(checked.error, "bad");
    field.focus();
    return;
  }
  await chrome.storage.sync.set({ [SETTING]: checked.url });
  field.value = checked.url;
  open.disabled = false;
  say("Saved. The toolbar button now opens this address.", "good");
});

open.addEventListener("click", async () => {
  const { url } = await readPortalUrl();
  if (url) chrome.tabs.create({ url });
});
