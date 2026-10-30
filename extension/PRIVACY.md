# Privacy: Archive address extension

The extension works only with the email archive you configure in its options.

**What it reads.** When you click its toolbar button or its "Use archive
address for this site" menu item, it reads the current tab's address, title and
site name (the `og:site_name` tag). It doesn't read pages otherwise, and has
no content scripts.

**What it sends, and where.** Only to the API address you entered: the page's
address (to look up your subscription for it), and, when you add or edit a
subscription, the name, description, sign-up link, tag and note in the form.
Requests carry the token you entered. Nothing goes to the extension's
developer or anyone else; there are no analytics.

**What it stores.** The API address, your token and which address form you
prefer, in your browser's local extension storage, which isn't synced to your
other devices. Remove the extension, or clear the options, to delete them.

**What it writes into pages.** When you choose Fill in (or the menu item),
your archive address is typed into one email field of that page.

The archive itself is run by its admin; what it keeps about your subscriptions
is up to them.
