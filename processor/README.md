# dea-capture

Archives what an email links to and embeds, and does so without acting as
the subscriber it was sent to.

Newsletters depend on remote content that rots: images on CDNs, the
articles they link to, PDFs, videos. `dea-capture` works out everything a
reader of a message would encounter and fetches it once. It never follows a
link that would change the subscription or act publicly in the subscriber's
name.

**Status: phase 0.** Extraction, classification and the safety guards
exist and are tested. Nothing fetches yet. The capture stages, which will run
as a [courier](https://github.com/CIRA-GEOIPS/courier) service, come next.

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

Embeds (images, `srcset`, `background=`, CSS `url()`, stylesheets) are
always fetched. That includes tracking pixels, which are flagged, because a
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

## Usage

```sh
uv sync
uv run dea-capture inventory message.eml           # full dry run, as JSON
uv run dea-capture inventory --summary --total *.eml  # counts only, no URLs
```

The full inventory contains the original hrefs, which often carry
per-subscriber tokens. Treat it as private. `--summary` output contains
no URLs.

## Development

```sh
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy src
```

`tests/fixtures/*.eml` are synthetic messages, one per sender pattern. Every
URL in them that must never be requested contains `SECRET` or `FORBIDDEN`.
The test fetcher fails on sight of either. `*.inventory.json` are golden
dry-run outputs. After a deliberate classifier change, regenerate them with
`UPDATE_GOLDEN=1 uv run pytest tests/test_golden.py` and review the diff.

Never add real messages as fixtures. They contain subscriber tokens.

## License

Apache-2.0.
