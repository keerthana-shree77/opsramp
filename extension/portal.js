// Where the portal lives, and what counts as a usable address.
//
// The address is not built into the extension. The same build is installed in
// front of a test deployment and a production one, and an address compiled in
// would have to be rebuilt and redistributed to move between them - so it is
// configuration, read at the moment the button is pressed.

export const SETTING = "portalUrl";

// Chrome hands the administrator's value to storage.managed. Reading that
// first is what makes a policy a policy: where IT has stated the address, a
// value left in this profile from before cannot quietly win.
export async function readPortalUrl() {
  try {
    const managed = await chrome.storage.managed.get(SETTING);
    if (managed && managed[SETTING]) {
      return { url: String(managed[SETTING]), locked: true };
    }
  } catch (error) {
    // No policy is configured on this machine, which is the ordinary case.
  }
  const local = await chrome.storage.sync.get(SETTING);
  return { url: local[SETTING] ? String(local[SETTING]) : "", locked: false };
}

// Returns the address to store, or an explanation of why it cannot be used.
// Plain HTTP is refused except on the loopback host: the portal carries an
// authenticated session, and the extension should not be the reason somebody
// sends one across the network in the clear.
export function checkPortalUrl(raw) {
  const text = (raw || "").trim();
  if (!text) return { error: "Enter the address of the portal." };

  let parsed;
  try {
    parsed = new URL(text);
  } catch (error) {
    return { error: "That is not a complete web address - include https://" };
  }

  const loopback = ["localhost", "127.0.0.1", "[::1]"].includes(parsed.hostname);
  if (parsed.protocol === "http:" && !loopback) {
    return { error: "Use https:// - the portal signs you in, so the address must be encrypted." };
  }
  if (parsed.protocol !== "https:" && parsed.protocol !== "http:") {
    return { error: "Use an http:// or https:// address." };
  }
  if (parsed.username || parsed.password) {
    return { error: "Remove the username and password from the address." };
  }
  if (!parsed.hostname) {
    return { error: "The address is missing a host name." };
  }
  return { url: parsed.href };
}
