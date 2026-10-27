# dea-capture

Archives what an email links to and embeds, and does so without acting as
the subscriber it was sent to.

Newsletters depend on remote content that rots: images on CDNs, the
articles they link to, PDFs, videos. `dea-capture` works out everything a
reader of a message would encounter and fetches it once. It never follows a
link that would change the subscription or act publicly in the subscriber's
name.

**Status: phase 1.** Extraction, classification and the safety guards
exist and are tested. The first capture stage, `issue`, fetches and stores
every embed of each archived message. It runs as a
[courier](https://github.com/CIRA-GEOIPS/courier) service. Link capture
comes in later phases.

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

## Running it as a service

`deploy/service.yaml` is the courier service: an S3 poller on
`incoming/archive/`, one job per `.eml`, and a local dispatcher that runs
`dea-capture issue`. It needs `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`
and `AWS_ENDPOINT_URL` (the R2 S3 endpoint) in the environment. The object
key is passed as its own argument, never through a shell.

The `Dockerfile` builds it with courier pinned to a commit (the `service`
extra). GitHub Actions publishes `ghcr.io/<owner>/dea-capture` for
linux/amd64 on every push to `main` that touches `processor/`.

Courier never restarts a crashed plugin, and the process keeps looking
healthy. Use `dea-capture healthcheck` as the liveness probe. It fails
when the poller's last completed scan, from courier's metrics on port 8000,
is older than `--max-scan-age` (default 900 s). Give it a startup probe
with room for the first full scan.

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
