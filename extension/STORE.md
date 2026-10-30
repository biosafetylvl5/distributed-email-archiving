# Store listing text

## Chrome Web Store

- **Name:** Archive address
- **Summary:** Add newsletters to your email archive and fill your archive address into their sign-up forms.
- **Category:** Productivity. **Visibility:** Unlisted.
- **Description:**
  For people with an account on a self-hosted newsletter archive. On a
  newsletter's sign-up page, click the extension to add the newsletter to your
  archive and get its address, then copy it or fill it into the form. You can
  also right-click an email field and choose "Use archive address for this
  site", and browse, search and edit your subscriptions. Needs the archive's
  API address and a token from its admin.
- **Single purpose:** adding newsletter subscriptions to the user's own email
  archive and entering the matching address into sign-up forms.
- **Permission justifications:**
  - `activeTab`: read the clicked tab's address, title and site name to suggest
    a name for the newsletter, and fill the address into its email field.
  - `scripting`: run those two small functions in the clicked tab only.
  - `storage`: keep the API address and the user's token on this device.
  - `contextMenus`: the "Use archive address for this site" item on form fields.
  - Host permission: call the user's archive API, at the address they configure.
- **Remote code:** none.
- **Data usage:** "Web history" (the page address of a sign-up page is sent to
  the user's own archive when they use the extension) and "Authentication
  information" (the user's API token, stored locally). Not sold, not used for
  anything else, not transferred to third parties.
- **Privacy policy:** link to `PRIVACY.md` in the repository.
- **Screenshots:** at least one, 1280×800 or 640×400.

## Firefox (addons.mozilla.org, unlisted)

Unlisted add-ons need no listing. The manifest declares
`data_collection_permissions: browsingActivity` (the sign-up page's address,
sent to the user's own archive). Upload the source zip when asked: the
JavaScript is bundled by esbuild (unminified) from `src/`; build with
`npm ci && npm run package`.
