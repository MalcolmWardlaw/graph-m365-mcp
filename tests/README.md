# Credential-layer tests

These cover `CliKeychainPersistence` in `server.py`: the code that reads and
writes the MSAL token cache through `/usr/bin/security`, plus
`test_attachment_extract.py` for the in-memory attachment extractors. Nothing
else in the server is tested here.

Run them with `uv run --script`; each file carries its own PEP 723 header, so
there is no venv to place and no dependency flags to pass.

```fish
uv run --script tests/test_cli_keychain.py    # unit, against a stand-in binary
uv run --script tests/test_real_keychain.py   # end to end, real login Keychain
uv run --script tests/test_attachment_extract.py  # in-memory extractors, no Graph
uv run --script tests/test_integration.py     # two caches over one item set
uv run --script tests/test_diagnose.py        # diagnostics: parser, classifier, check table
uv run --script tests/test_draft_attachments.py   # create_draft attachment path rules; LIVE=1 for the Graph round trip
uv run --script tests/test_daemon.py          # daemon + shim: real daemon under a throwaway service, sandbox acceptance
```

| File | What it covers |
|------|----------------|
| `test_cli_keychain.py` | 21 cases against `fake_security.py`: round trip, chunking, the torn-set and junk-staging fallbacks, the over-length refusal, ACL flags, exit-status handling, timeouts |
| `fake_security.py` | Stand-in for `security(1)`. Enough of find/add/delete-generic-password to drive the above, including its stderr-on-success behavior |
| `test_real_keychain.py` | The same round trip against the real login Keychain, under a throwaway service name that is swept in a `finally` block |
| `test_integration.py` | Two `PersistedTokenCache` instances over one item set, as the live server processes are. Covers modify to save and search to reload to load |
| `test_diagnose.py` | The `diagnose` tool and the classifier behind connection errors: DNS wire parser on a hand-built response, the three failure shapes with the resolver API monkeypatched, the full six-row table offline, one live direct-DNS query skipped when offline |
| `test_draft_attachments.py` | The path rules behind `create_draft(attachments=)`: roots, symlink escape, size ceilings, inline payload shape, validation before any Graph call. With `LIVE=1` and the Graph env set, a real draft with a 10 KB and a 5 MB file (upload session), then the induced-failure rollback; leaves two empty drafts in Recoverable Items |
| `test_daemon.py` | A real `server.py --daemon` under `KEYCHAIN_SERVICE=graph-m365-daemon-test`: socket mode, lock semantics, second-daemon refusal, the Keychain-backend refusal, shim round trips (single, concurrent, `-X importtime` to prove the shim imports no MSAL/httpx/MCP), in-process fallback with no daemon, refusal to fall back while the lock is held, SIGTERM cleanup, and the roadmap acceptance test under a `sandbox-exec` profile that denies all network except the one socket. Cleans up its config directory |
| `measure_line_limit.py` | Binary-searches `security -i` for the exact command-line ceiling. Backs `_LINE_LIMIT`; run it if that constant is ever doubted |

Two things to preserve when editing these.

**Size the payload like a real cache.** The 4096-byte truncation shipped
because the write path was verified with a short synthetic payload, which fits
a single item and so never reached the ceiling. A payload under roughly 3 KB of
JSON proves nothing about the path that runs. Use incompressible token blobs,
not repeated characters, since a real cache is mostly opaque base64.

**Never touch the real service name.** Every test asserts its service name is
not `graph-m365` before doing anything, and sweeps its items afterwards. The
real-Keychain tests create and delete items in your login Keychain; they must
leave it exactly as they found it.
