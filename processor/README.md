# dea-capture

Archives what an email links to and embeds, and does so without acting as
the subscriber it was sent to.

Newsletters depend on remote content that rots: images on CDNs, the
articles they link to, PDFs, videos. `dea-capture` works out everything a
reader of a message would encounter and fetches it once. It never follows a
link that would change the subscription or act publicly in the subscriber's
name.

**Status: phase 3.** Extraction, classification and the safety guards exist
and are tested. The `issue` stage stores every embed of each archived
message, then resolves its links and queues each landing page once.
`capture` stores direct downloads (PDFs, images, files), pages, which it
loads in [browsertrix-crawler](https://github.com/webrecorder/browsertrix-crawler),
video and audio posts, which it downloads with
[yt-dlp](https://github.com/yt-dlp/yt-dlp), and social media posts, which it
collects with [gallery-dl](https://github.com/mikf/gallery-dl), all behind a
guarding proxy. It
retries failures on a schedule and falls back to the Wayback Machine. It all
runs as a [courier](https://github.com/CIRA-GEOIPS/courier) service.

## What it does with each link

| Class | Example | Decision |
|---|---|---|
| `content` | an article, PDF, video page | **capture** the canonical URL |
| `tracking-redirect` | `list-manage.com/track/click`, Substack `/redirect/…` | **resolve**: follow the redirect chain as a click would, checking every hop |
| `login-token` | links carrying `token=` or `access_token=` | **capture** the canonical URL with the token removed, which gives the logged-out view |
| `unsubscribe` | unsubscribe, preferences, `List-Unsubscribe` header URIs | **skip** |
| `action` | confirm, verify, RSVP, vote and poll answers | **skip** |
| `non-web` | `mailto:`, `tel:`, `javascript:` | **skip** |
| `internal-anchor` | `#top` | **skip** |

Rules about what not to fetch run first. Where a rule is unsure, it errs
towards **skip**: a skipped link is recorded and can be captured by hand,
but a fetched unsubscribe link can't be undone. Some redirects carry their
target in the URL, such as Substack's `/redirect/2/<jwt>`. Those are decoded
offline, so they can be classified and deduplicated before any request.

Embeds (images, `srcset`, `background=`, video posters, image inputs, icon
links, CSS `url()` and `@import`, stylesheets) are always fetched. That includes tracking pixels, which are flagged, because a
reader's mail client loads them too.

## Safety guards

Every request goes through `guard.GuardedFetcher`, whatever the classifier
decided:

- **Links and redirect hops:** anything that classifies as unsubscribe,
  action, non-web or an in-message anchor is refused, and so is anything
  still carrying an auth parameter.
- **All requests:** only `http` and `https`, and no credentials in the URL.
  The host must resolve only to public addresses. Private, loopback,
  link-local, CGNAT, ULA, multicast and metadata ranges are refused, as are
  any extra ranges you configure, such as cluster CIDRs. The checked
  addresses are passed to the fetcher so it can pin the connection to them.

`fetch.HttpFetcher` connects to those addresses and never resolves the name
again; TLS still checks the certificate against the original host. It never
follows redirects itself. `fetch.follow` sends every hop back through the
guard, so a click tracker that lands on an unsubscribe page is refused before
that request is made.

A page's browser makes its own requests, so it gets the same address check
from `proxy.GuardProxy`, a forward proxy on `127.0.0.1` that the crawler is
pointed at. The browser is started with `--proxy-bypass-list=<-loopback>`,
so `localhost` goes through the proxy too and is refused (the crawler's
redis and the browser's debugging port listen there). The proxy tunnels TLS
on port 443 and forwards plain HTTP on port 80, and refuses everything else.
browsertrix-crawler turns off the browser's certificate checks whenever a
proxy is set, so the proxy checks instead: before the first tunnel to a host
and address, it completes its own verified TLS handshake with them.

## The `issue` stage

`dea-capture issue s3://bucket/incoming/archive/<key>.eml` writes, under
`issues/<sha256 of the .eml>/` in the same bucket:

- `links.json`: the classified link inventory. Private: it has the original
  hrefs.
- `assets/<sha256 of body><ext>`: each embed body and inline `cid:` part.
- `assets/manifest.json`: one record per embed, with its status (`ok`,
  `failed`, `refused`, `deferred:size`, `deferred:budget`), redirect chain,
  size and type. A message with a current manifest is done and is skipped.
  A manifest from an older `EXTRACT_VERSION` is redone incrementally: embeds
  it already stored are kept, and only new URLs are fetched.

After the manifest it writes `issue-status/<sha256>.json`: counts only (embeds
by status, tracking pixels, links by decision), the source key and `state`
(`done`, or `partial` while any embed failed or was deferred). The same
values are set as the object's metadata, so the admin dashboard reads them
from one R2 listing. A message skipped as done gets its status file if it
has none.

Then the links. Each distinct `resolve` href is followed once, reading
status lines and headers only, with every hop checked by the guard. A hop
that carries an auth parameter, or any parameter whose value is an email
address, is never requested: the chain stops there and its canonical URL
counts as the landing page. Each landing page, and each
`capture` link's canonical URL, is queued for capture (see below). The
results go to `issues/<sha256>/captures.json`, private like `links.json`, and
the status file gains `links_state` (`pending`, `done`, or `partial` when a
redirect couldn't be followed) with counts of what was resolved and queued.
A message whose embeds are already done still gets its links resolved, once.
`--no-links` skips this half.

Last, `issues/<sha256>/view/`: the message as the admin dashboard shows it.
`index.html` is the HTML part (or the text part, escaped) with every embed,
`srcset` and CSS `url()` pointing at the stored copy, and stored stylesheets
copied to `css/` with their own references rewritten the same way. An embed
that wasn't stored, and every tracking pixel, is dropped. A link points at
where it was resolved to, or at its canonical URL for a `capture` link, and
opens in a new tab; any other link (a tracker that wasn't followed,
unsubscribe and other per-reader links, `mailto:`) loses its `href`. Scripts,
frames, forms, `<meta>`, `<base>` and event handlers are removed. `meta.json`,
written last, has the headers the dashboard shows, the MIME parts (types and
sizes), sizes and what was removed, by count. It is rendered again whenever
the links change, and for a message skipped as done if it has none or it's
from an older `VIEW_VERSION`. A rendering failure is counted (`view_failed`)
and never stops the capture.

A redirect that couldn't be followed (a tracker that answered 429, say) is
not retried on its own. `dea-capture relink s3://<bucket>` follows those
again, message by message, at most one request per host every 10 s
(`--interval`), and stops at the first 429. Following a tracking link again
counts as another click on it, so run it deliberately. It rewrites
`captures.json` and the status record, and queues where the links land.

Stored stylesheets are read too. The fonts and images they refer to are
fetched as `css` embeds, resolved against the stylesheet's own URL, and their
`@import`s as `css-import`, up to three stylesheets deep. Each record notes
`found_in`, the stylesheet that referred to it. After 300 such files in one
message, the rest are recorded as `deferred:budget`.

Requests to one host are at least a second apart. Bodies over 50 MiB are
deferred, not fetched. `DEA_EXTRA_BLOCKED` (comma-separated CIDRs) adds
ranges to refuse, such as the cluster's pod and service networks.
`DEA_MODE=dry-run` makes it print counts and fetch nothing. Its stdout is one
line of counts per message, with no URLs.

## Link capture

Every canonical URL is captured once, however many messages link to it. With
`h` = sha256 of the canonical URL:

- `captures/index/<h>.json` is its record: kind (`page`, `direct`, `media` or
  `gallery`), status (`queued`, `retrying`, `ok`, `failed`, `refused`,
  `deferred`), every attempt, and where the good capture is. Status, kind and
  attempt count are also its metadata, for the dashboard. It is created
  with `If-None-Match: *`, so exactly one message queues each URL.
- `captures/due/<kind>/<UTC time>-<h>.json` means "capture `h` at or after
  this time". This is the queue and the retry schedule. A capture first
  moves its key to a lease an hour ahead, so one that dies runs again then.
- `captures/<h[:2]>/<h>/<time>/` holds one attempt's files: the body as
  `original<ext>` and `capture.json` with the status, headers, redirect
  chain, size and sha256.

`dea-capture capture s3://bucket/captures/due/direct/<key>.json` runs one.
If the body turns out to be HTML, the URL moves to the `page` queue. Bodies
over 100 MiB are `deferred`, and refusals by the guard are final. Failures
(network errors, 4xx and 5xx) are retried 1 h, 6 h, 1 d, 7 d and 30 d
later.

Each kind has its own size cap. A subscription can also have a lower limit,
set on the dashboard, which writes every subscription's limit to
`config/capture-limits.json` (bytes by `user/tag`). A URL's record lists the
subscriptions whose messages linked it. The highest of their limits applies,
and no limit applies if any of them has none, or a message without a
subscription linked it. Records from before limits existed have no limit. A
capture over its limit is `deferred` with the limit noted on the attempt.
Media and social posts stop downloading at the limit; pages and files are
checked once fetched. If a subscription with a higher limit, or none, links
a deferred URL later, it is queued again.

A file or page that fails also gets the Wayback Machine snapshot closest to
when the URL was first queued, found with the CDX API: at once if the site
answered 401, 403, 429 or 451, else after the second failure (most one-off
failures clear on the 1 h retry), else after the last. Its folder is the
record's `wayback.capture`. The retries go on, so a capture of our own can
still follow. A file's snapshot is its original bytes (`id_`). A page's is
crawled as Wayback replays it (`if_`, no toolbar) into a WACZ like any other
page, with `via: wayback:<timestamp>` and the `snapshot` URL in
`capture.json`; if that crawl fails, the original HTML is stored instead.

Page keys load the canonical URL alone in browsertrix-crawler (no other pages,
no links followed), with its scroll and autoplay behaviours, a 60 s load
timeout, 4 minutes in all and 200 MiB at most. The attempt's folder gets
`page.wacz`, the viewport `screenshot.png`, the extracted `text.txt` and
`capture.json` (status, title, the crawler's version and the proxy's counts).
Failures retry on the same schedule. The crawler's log, which names every URL
the page loaded, stays in a temporary directory; when the page fails, its
warnings and errors, with each URL cut to its host, are the attempt's error. `dea-capture page URL --out
DIR` captures one page locally, in the container image.

Media keys (video sites, audio and video files) go to yt-dlp behind the same
proxy. It first reads the post's metadata only. A channel or playlist moves
to the `page` queue, a live or upcoming stream is retried later, and a post
estimated at more than 1 GiB (best format up to 1080p) is `deferred`. Then it
downloads with its own downloader for every protocol, so ffmpeg only merges
files on disk. The folder gets `media.<ext>`, `thumbnail.<ext>`, `info.json`
(the post's metadata, without signed format URLs or request headers) and
`capture.json`; files stream from disk to the store. `dea-capture media URL
--out DIR` grabs one locally.

Gallery keys are single posts on X/Twitter, Bluesky, Instagram, Facebook,
Flickr, Tumblr and Imgur. gallery-dl collects the post's images and videos at
their original resolution, with no account or cookies: at most 100 files and
3 GiB. The folder gets `files/001.<ext>`... in the post's order, `info.json`
(the post's text, author and date, and each file's metadata, with signed CDN
URLs' query strings removed) and `capture.json`. A site that wants a login (Instagram,
usually), or a URL gallery-dl doesn't know, moves to the `page` queue, and
stays there. Posts queued as pages before this stage existed move to it the
first time their page key comes due. `dea-capture gallery URL --out DIR`
collects one locally.

A link whose tracker answered without redirecting (a 429, say) stays
unresolved: the tracker's URL carries the reader's token, so it is never
queued, and the capture stages refuse one without requesting it.

Only canonical URLs, with tracking and auth parameters and email addresses
removed, are stored under `captures/`.

## Running it as a service

`deploy/service.yaml` is the courier service. An S3 poller on
`incoming/archive/` makes one job per `.eml` for `dea-capture issue`. An
`s3_due_poller` each on `captures/due/direct/`, `captures/due/page/` and
`captures/due/media/` and `captures/due/gallery/` makes
one job per due key for `dea-capture capture`. That poller is courier's S3
poller, except that it emits a key only once the time in its name has passed. It comes with this
package as a courier plugin. The service needs `AWS_ACCESS_KEY_ID`,
`AWS_SECRET_ACCESS_KEY` and `AWS_ENDPOINT_URL` (the R2 S3 endpoint) in the environment. The object
key is passed as its own argument, never through a shell. The crawler,
yt-dlp and gallery-dl get the environment without the `AWS_*` credentials.

The `Dockerfile` builds it on the browsertrix-crawler image, with courier
pinned to a commit (the `service` extra). GitHub Actions publishes
`ghcr.io/<owner>/dea-capture` for linux/amd64 on every push to `main` that
touches `processor/`, after `deploy/smoke-page.py` passes inside the image:
the browser, yt-dlp and gallery-dl can't reach a server on loopback, and a real page
captures. One page capture at a time wants about 2 CPUs and 2 GiB of memory,
and up to 1 GiB in `/tmp`; one media capture up to about 2 GiB in `/tmp`
while it merges a 1 GiB post, and a social post up to about 3 GiB.

Courier never restarts a crashed plugin, and the process keeps looking
healthy. Use `dea-capture healthcheck` as the liveness probe. It fails
when any poller's last completed scan, from courier's metrics on port 8000,
is older than `--max-scan-age` (default 900 s). Give it a startup probe
with room for the first full scan.

Every 5 minutes the service also runs `dea-capture summary s3://<bucket>`.
It lists the archive, `issue-status/` and `captures/index/`, and writes
`dashboard/summary.json`: the counts and metadata the admin dashboard shows,
which the Worker can't afford to list itself. Only objects whose ETag changed
since the last run get a HEAD request; what it has seen is kept in
`dashboard/summary-state.json`. Recipient addresses and URLs are left out,
and of each `issue-status/` record only the fields the dashboard reads, less
counts of 0. Both files are compact JSON, as the Worker parses the summary on
every page view.

Once an hour it also lists the whole bucket and adds `storage` to the
summary: objects and bytes by area (archived and quarantined mail, embeds,
rendered views, link records, link captures by kind, the capture index and
queue), and the bytes under each message's `issues/<sha256>/`. In between,
the last count is carried over.

Before that, it applies the dashboard's retry requests. The Worker never
edits a capture record, as a running capture would overwrite the edit. It
writes `captures/retry/<stamp>-<id>.json` instead: either every URL of one
kind and status (`retrying` or `failed`), or one message, whose failed
redirects are followed again (as `relink` does) before its URLs are retried.
A retried URL is due at once; a `failed` one goes back to `retrying` for one
more attempt. One due within its kind's lease time is left alone, since it
may be running. Requests get 5 minutes per run, and one cut short waits for
the next. What each did (counts only) goes to `dashboard/retries.json` and
the summary. `--no-retries` skips this.

## Usage

```sh
uv sync
uv run dea-capture inventory message.eml           # full dry run, as JSON
uv run dea-capture inventory --summary --total *.eml  # counts only, no URLs
uv run dea-capture issue message.eml --out ./out    # fetch embeds into ./out
uv run --extra service courier run deploy/service.yaml
```

The full inventory contains the original hrefs, which often carry
per-subscriber tokens. Treat it as private. `--summary` output contains
no URLs.

## Development

```sh
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy src tests
uv run --extra service courier validate deploy/service.yaml
```

`tests/fixtures/*.eml` are synthetic messages, one per sender pattern. Every
URL in them that must never be requested contains `SECRET` or `FORBIDDEN`.
The test fetcher fails on sight of either. `*.inventory.json` are golden
dry-run outputs. After a deliberate classifier change, regenerate them with
`UPDATE_GOLDEN=1 uv run pytest tests/test_golden.py` and review the diff.

Never add real messages as fixtures. They contain subscriber tokens.

## License

Apache-2.0.
