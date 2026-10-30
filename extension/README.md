# Archive address (browser extension)

Adds newsletters to your email archive from their sign-up pages, and types your
archive address (`you+tag@…`) into their forms. It talks to the ingest Worker's
[subscription API](../README.md#subscription-api) with a token the archive's
admin makes for you on the dashboard.

- **Popup, on a sign-up page:** shows your subscription for this page (or
  others on the same site); otherwise suggests a name and tag and adds it, with
  a description, sign-up link and note under "More". Each subscription has
  Copy, Fill in (into the page's email field), Edit and Sign-up page.
- **All subscriptions:** a searchable list; Edit changes the name,
  description, sign-up link and your note. Status, sender domain and capture
  limit are the admin's and shown read-only. An edit to a copy that has since
  changed is refused, and the latest is shown instead.
- **Right-click on a form field:** "Use archive address for this site" finds or
  adds this site's subscription and types its address into that field.

One codebase, Manifest V3, for Chrome (121+) and Firefox (140+).

## Permissions

| Permission | Why |
|---|---|
| `activeTab`, `scripting` | read the page's address, title and site name, and fill the email field, only on the tab you click the extension or its menu item in |
| `storage` | the API address, token and address form, in this browser only (`storage.local`) |
| `contextMenus` | the right-click item on form fields |
| host permission for the API's origin | to call the API; granted at install when built with `API_URL`, else asked for in the options (`optional_host_permissions`) |

There are no content scripts, no analytics, and nothing is sent anywhere but
the configured API. See [PRIVACY.md](PRIVACY.md).

## Build

```sh
npm ci
npm run typecheck && npm test
API_URL=https://mail-api.example.org npm run package   # or without API_URL: entered in the options
npm run lint:firefox
```

`dist/chrome/` and `dist/firefox/` are the unpacked extensions (load them from
`chrome://extensions` with developer mode, or `about:debugging` in Firefox).
`npm run package` also writes `dist/archive-address-{chrome,firefox}-<version>.zip`
and `dist/archive-address-source-<version>.zip`, the source for Mozilla's review.
The bundles aren't minified. Icons are drawn by `scripts/make-icons.py`.

## Distribution (signed, unlisted)

- **Firefox:** sign it on addons.mozilla.org as an unlisted add-on, with API
  credentials from <https://addons.mozilla.org/developers/addon/api/key/>:
  ```sh
  WEB_EXT_API_KEY=… WEB_EXT_API_SECRET=… npx web-ext sign --channel unlisted \
    --source-dir dist/firefox --upload-source-code dist/archive-address-source-<version>.zip
  ```
  This writes a signed `.xpi` to `web-ext-artifacts/`, which installs from a
  file or a link. Bump `version` in `package.json` for every upload.
- **Chrome:** upload `dist/archive-address-chrome-<version>.zip` in the
  [Chrome Web Store developer dashboard](https://chrome.google.com/webstore/devconsole)
  with visibility "Unlisted"; only people with the link can install it.
  [STORE.md](STORE.md) has the listing text and the permission justifications.
