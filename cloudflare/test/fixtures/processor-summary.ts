export default {
  "version": 1,
  "generated_at": "2026-10-27T12:00:00+00:00",
  "incoming": [
    {
      "key": "incoming/archive/2026-10-25/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.eml",
      "size": 100,
      "uploaded": "2026-10-25T08:00:00+00:00",
      "customMetadata": {
        "user": "ann",
        "tag": "news",
        "envelope_from": "writer@example.com"
      }
    },
    {
      "key": "incoming/archive/2026-10-26/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.eml",
      "size": 50,
      "uploaded": "2026-10-26T08:00:00+00:00",
      "customMetadata": {
        "user": "ann",
        "tag": "news",
        "envelope_from": "writer@example.com"
      }
    },
    {
      "key": "incoming/quarantine/2026-10-26/cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc.eml",
      "size": 7,
      "uploaded": "2026-10-26T09:00:00+00:00",
      "customMetadata": {
        "user": "bob"
      }
    },
    {
      "key": "incoming/archive/2026-08-01/dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd.eml",
      "size": 20,
      "uploaded": "2026-08-01T08:00:00+00:00",
      "customMetadata": {
        "user": "ann",
        "tag": "news",
        "envelope_from": "other@example.org"
      }
    }
  ],
  "statuses": [
    {
      "key": "issue-status/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.json",
      "customMetadata": {
        "source": "incoming/archive/2026-10-25/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.eml",
        "state": "done",
        "links_state": "partial",
        "completed_at": "2026-10-25T10:00:00+00:00",
        "assets_ok": "7",
        "links_unresolved": "2"
      }
    }
  ],
  "urls": {
    "byKind": {
      "page": {
        "ok": 2
      }
    },
    "wayback": 1,
    "lastUpdated": "2026-10-27T10:00:00+00:00"
  },
  "aggregates": {
    "stats": {
      "partial": false,
      "totals": {
        "archive": {
          "count": 3,
          "bytes": 170
        },
        "quarantine": {
          "count": 1,
          "bytes": 7
        }
      },
      "last7": {
        "archive": 2,
        "quarantine": 1
      },
      "last30": {
        "archive": 2,
        "quarantine": 1
      },
      "tags": {
        "bob/": {
          "user": "bob",
          "tag": "",
          "archive": 0,
          "quarantine": 1,
          "bytes": 7,
          "last": "2026-10-26T09:00:00+00:00"
        },
        "ann/news": {
          "user": "ann",
          "tag": "news",
          "archive": 3,
          "quarantine": 0,
          "bytes": 170,
          "last": "2026-10-26T08:00:00+00:00"
        }
      },
      "oldestQuarantine": "2026-10-26",
      "days": [
        {
          "day": "2026-10-14",
          "archive": 0,
          "quarantine": 0
        },
        {
          "day": "2026-10-15",
          "archive": 0,
          "quarantine": 0
        },
        {
          "day": "2026-10-16",
          "archive": 0,
          "quarantine": 0
        },
        {
          "day": "2026-10-17",
          "archive": 0,
          "quarantine": 0
        },
        {
          "day": "2026-10-18",
          "archive": 0,
          "quarantine": 0
        },
        {
          "day": "2026-10-19",
          "archive": 0,
          "quarantine": 0
        },
        {
          "day": "2026-10-20",
          "archive": 0,
          "quarantine": 0
        },
        {
          "day": "2026-10-21",
          "archive": 0,
          "quarantine": 0
        },
        {
          "day": "2026-10-22",
          "archive": 0,
          "quarantine": 0
        },
        {
          "day": "2026-10-23",
          "archive": 0,
          "quarantine": 0
        },
        {
          "day": "2026-10-24",
          "archive": 0,
          "quarantine": 0
        },
        {
          "day": "2026-10-25",
          "archive": 1,
          "quarantine": 0
        },
        {
          "day": "2026-10-26",
          "archive": 1,
          "quarantine": 1
        },
        {
          "day": "2026-10-27",
          "archive": 0,
          "quarantine": 0
        }
      ],
      "senders": [
        {
          "domain": "example.com",
          "count": 2
        }
      ]
    },
    "captures": {
      "partial": false,
      "messages": [
        {
          "key": "incoming/archive/2026-10-26/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.eml",
          "day": "2026-10-26",
          "user": "ann",
          "tag": "news",
          "capture": null
        },
        {
          "key": "incoming/archive/2026-10-25/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.eml",
          "day": "2026-10-25",
          "user": "ann",
          "tag": "news",
          "capture": {
            "sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "source": "incoming/archive/2026-10-25/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.eml",
            "state": "done",
            "linksState": "partial",
            "completedAt": "2026-10-25T10:00:00+00:00",
            "counts": {
              "assets_ok": 7,
              "assets_failed": 0,
              "assets_refused": 0,
              "assets_deferred": 0,
              "pixels": 0,
              "links": 0,
              "links_capture": 0,
              "links_resolve": 0,
              "links_skip": 0,
              "links_resolved": 0,
              "links_unresolved": 2,
              "links_refused": 0,
              "captures_queued": 0
            }
          }
        },
        {
          "key": "incoming/archive/2026-08-01/dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd.eml",
          "day": "2026-08-01",
          "user": "ann",
          "tag": "news",
          "capture": null
        }
      ],
      "states": {
        "done": 1,
        "partial": 0,
        "pending": 2
      },
      "linkStates": {
        "done": 0,
        "partial": 1,
        "pending": 0
      },
      "totals": {
        "assets_ok": 7,
        "assets_failed": 0,
        "assets_refused": 0,
        "assets_deferred": 0,
        "pixels": 0,
        "links": 0,
        "links_capture": 0,
        "links_resolve": 0,
        "links_skip": 0,
        "links_resolved": 0,
        "links_unresolved": 2,
        "links_refused": 0,
        "captures_queued": 0
      },
      "lastCompleted": "2026-10-25T10:00:00+00:00",
      "bySubscription": {
        "ann/news": {
          "done": 1,
          "partial": 0,
          "pending": 2
        }
      }
    }
  }
};
