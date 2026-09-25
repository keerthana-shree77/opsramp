# Firmware Compliance Portal - Chrome extension

A toolbar button that opens the portal. That is the whole of it.

It reads nothing from any page, injects nothing into any page, and sends
nothing anywhere. It asks for one permission, `storage`, and that is only so
it can remember the portal's address between sessions.

| Not requested | Why it is not needed |
|---|---|
| `tabs` | `chrome.tabs.create()` opens a tab without it. The permission is for *reading* tabs - their addresses, their titles - which this does not do. |
| `host_permissions` | Nothing is read from the portal or from any other site. The extension navigates to an address; the page then belongs to the browser, as if you had typed it. |
| `activeTab`, `scripting` | No script is ever injected. |
| `identity`, `cookies` | Sign-in is the portal's business and the browser's. The extension takes no part in it and never sees a credential. |

## The address is configuration, not code

**There is no address built into this extension.** The same build is meant to
sit in front of a test deployment and a production one, and an address
compiled in would have to be rebuilt and redistributed to move between them.

It is read, in this order:

1. **Chrome policy** (`storage.managed`) - what an administrator has pushed.
   When this is set the options page shows it and will not let anyone change
   it.
2. **The user's own setting** (`storage.sync`) - typed on the options page,
   and carried to their other signed-in Chrome profiles.

If neither is set, pressing the button opens the options page rather than a
blank tab.

An address must be `https://`, with no username or password in it. Plain
`http://` is refused except on `localhost` and `127.0.0.1`, which is there so
a developer can point it at `python app.py`. The portal signs you in, and the
extension should not be the reason a session crosses the network in the clear.

## Building it

There is no build step. It is plain JavaScript, HTML and CSS, loaded as it is
written. The icons are the only generated files, and they are committed - to
redraw them:

```bash
python ../tools/make_extension_icons.py
```

(run from the `extension/` directory - it writes `icons/icon{16,32,48,128}.png`)

## Installing it while you work on it

1. Open `chrome://extensions`.
2. Turn on **Developer mode** (top right).
3. **Load unpacked**, and choose this `extension/` directory.
4. The options page opens on first install. Enter the portal's address.

After editing a file, press **Reload** on the extension's card. Editing
`manifest.json` or `background.js` needs that reload; editing the options page
only needs the page reopened.

## Distributing it to users

**Confirm the route with HPE IT before packaging.** Chrome offers two, and
which one applies is an HPE decision, not a technical one:

**Force-installed by policy from an internal host.** The extension is packed
to a `.crx`, published on an internal web server together with an update
manifest, and pushed by the `ExtensionInstallForcelist` policy. Users get it
without acting, and it cannot be removed by hand. This is the usual answer in
a managed fleet, and the one that pairs with setting the address by policy.

**An internal extension catalogue,** if HPE runs one. Same packaging, their
publishing process.

**Not the public Chrome Web Store.** This is an internal tool whose whole
purpose is an address on the internal network. Publishing it publicly would
put that address in a public listing. If HPE explicitly wants it there, that
is their call to make in writing.

### Setting the address by policy

Once the extension has an ID (packing assigns one from the signing key), the
address can be pushed with it, so nobody has to be told what to type.

On Windows, under
`HKLM\Software\Policies\Google\Chrome\3rdparty\extensions\<EXTENSION_ID>\policy`:

| Value | Type | Data |
|---|---|---|
| `portalUrl` | `REG_SZ` | the portal's address |

On Linux, `/etc/opt/chrome/policies/managed/firmware-portal.json`:

```json
{
  "3rdparty": {
    "extensions": {
      "<EXTENSION_ID>": {
        "portalUrl": "https://<the portal's address>"
      }
    }
  }
}
```

The schema these validate against is `managed_schema.json` in this directory.
Check what arrived at `chrome://policy`.

## Updating it

Raise `version` in `manifest.json`, repack, and replace the `.crx` and the
update manifest on the internal host. Chrome checks for updates on its own
schedule; `chrome://extensions` → **Update** forces it.

## Removing it

Force-installed extensions cannot be removed by the user: take the entry out
of `ExtensionInstallForcelist` and Chrome removes it at the next policy
refresh. A copy loaded unpacked is removed with **Remove** on its card.

Nothing is left behind but the stored address, which goes with it.
