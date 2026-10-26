# Browser collection development path

The Papers screen has **브라우저에서 논문 수집**. This is a source development
candidate, not a released or fully browser-qualified feature. The implemented
first adapter accepts modern, versioned arXiv identifiers. Authenticated publisher
sites, older arXiv identifiers, automated schedules and multiple-browser support
are follow-up work. Do not infer their acceptance from synthetic tests.

## Workflow

1. Start the local app with a fresh or deliberately selected `XRML_DATA_DIR` and
   the existing local authentication mode. Open a writable topic and Papers.
2. Enter an arXiv query and explicitly search. Only the query goes to the official
   metadata API; up to five candidates are shown. Select papers and save a job.
3. Prepare the development extension/native host below. In the app, issue a
   connection code for the selected job, click **연결 코드 복사**, and paste it into
   the extension popup. Saving a job only queues it; it does not download a PDF.
   The code lasts one hour; issuing another code revokes the old one. App-side
   revocation stops further bridge access. Codes are never site login cookies.
4. In the extension, click the individual paper's download button. The browser
   uses its existing site session through its Downloads API. No cookies, passwords,
   browser profiles, unrelated tabs or general download history are copied.
5. Refresh/reconcile progress. Completion requires the registered download ID,
   extension ownership, approved version URL, exact filename and byte counts.
   The host reads only that attempt under the configured `Downloads/XRML` subtree.
   The app then checks SHA-256, actual PDF parsing, page/size limits and truncation.
6. In the app, open the validated PDF and compare title, authors and version with
   the selected paper. Confirm the association to save it into Papers. This
   confirmation is user provenance, not independent bibliographic verification.
7. Open the saved paper, extract its attached PDF and inspect page coverage. Use
   existing document preparation, analysis approval, saved analysis and Journal
   adoption/export flows. Acquisition never issues AI-call authority.

The app stores query, candidates, exact version URLs, job creation time, individual
attempt/download IDs, file hash/size, collection time and status. Searches and
downloads do not auto-start when reopening the app. Browser restart clears the
extension's session-only capability; issue a fresh code and reconcile the persisted
download IDs. The app job survives separately on disk.

Connection codes belong in the extension's **앱의 작업 연결 코드** field, never in
the arXiv query. The UI and API reject recognized connection/credential-shaped
queries before searching or saving. Legacy affected query displays are redacted
and cannot issue a new connection, while job inspection and revocation remain
available. This bounded paste check is not a general detector of all private data.

## Preparing the extension and native host

Requirements: Windows, a local source checkout, Python with the project's locked
dependencies and optional `fulltext` extra, and a compatible Manifest V3 browser.
The observed implementation target is Whale 5.39.412.57; that version observation
does not prove Native Messaging registration or an actual download worked.

The unpacked extension is `extensions/whale-collector`. Loading it into a browser
and registering a native host are explicit installation actions. Preparation does
not modify the registry or any existing browser profile. Do not change browser
security policy to force installation. Prefer an isolated test profile first.

After the browser assigns the extension ID, run from the repository root:

```powershell
uv sync --frozen --extra dev --extra fulltext
uv run python scripts/prepare_browser_native_host.py `
  --browser whale `
  --extension-id YOUR_EXACT_EXTENSION_ID `
  --download-root "$env:USERPROFILE\Downloads\XRML" `
  --output "$env:LOCALAPPDATA\XRML\collector-registration"
```

Use the browser's actual download folder if it differs from Windows Downloads.
The output directory must be new. Inspect `host-config.json`, the host manifest,
`launch.cmd` and `register-current-user.reg` before applying registration yourself.
The manifest permits exactly the specified extension origin. The current Whale
preparation binds `whale-extension://YOUR_EXACT_EXTENSION_ID/` and the user key
`Software\Naver\Naver Whale\NativeMessagingHosts\org.xrml.paper_collector`.
Chrome and Edge use `chrome-extension://` with their own distinct registry keys.
The host validates both the selected browser and exact persisted origin before
reading a request. Configurations prepared before this browser binding must be
regenerated in a new directory; unbound old configurations are rejected.
The registry string was checked against the installed signed vendor binary.
Whale documents this scheme for extension pages; acceptance by its native host
manifest parser is still unconfirmed. Preparation is not proof of a successful
browser invocation/download.
The first actual test-profile connection returned **Specified native messaging
host not found** despite the registered key and manifest being present. Its cause
remains unresolved; do not treat this development path as browser-qualified.
Chrome/Edge preparation support is not a compatibility claim.
No native host is bundled into the desktop installer in this checkpoint.

App connection codes contain an IPv4 loopback address/port, job ID and revocable
capability. They are shown only after an explicit action. Do not publish them or
screenshots containing them. The host bypasses proxy configuration for this local
transport and refuses redirects. It cannot invoke arbitrary shell commands or
read files outside the exact attempt path.

## Recovery and limits

- An interrupted item stays in the job. Resolve login/MFA/CAPTCHA manually, then
  prepare resume in the app and explicitly start a new attempt in the extension.
  This state-machine behavior is fixture-tested; arXiv is not an authenticated
  publisher acceptance test. No automation solves authentication challenges.
- App **접수 중단** fences late uploads; it does not promise to cancel the browser's
  underlying network transfer. Cancel that download in the browser if needed.
- An unknown dispatch cannot silently download again. Check browser history and
  explicitly confirm a new attempt. A lost upload response is reconciled by ID
  and hash. A lost final save response can be retried without another attachment.
- HTML login pages, partial/repaired/encrypted PDFs, dangerous downloads, redirects
  to other versions/hosts and files outside the fixed path are rejected. A rejected
  PDF is not full text. The existing 25,000,000-byte and 2,000-page limits apply.
- Identical version + PDF content can reuse an existing attachment. A different
  version remains distinct; changed bytes for a version are not silently replaced.
- First bridge issuance supports local auth only. Shared/OIDC connections are
  rejected until a principal/revocation design is qualified. Ordinary topic UPDATE
  permission remains required on app control and every capability operation.
- Browser APIs do not enforce tab isolation for the application. This first adapter
  requests no tab-content access and operates only on app-selected URLs/downloads.
  It is not the Codex browser MCP download mechanism.

## Verification

```powershell
uv run python -m pytest tests/test_browser_collection.py -q
uv run python -m pytest --noconftest tests/test_browser_native_host.py -q
node --test extensions/whale-collector/protocol.test.js extensions/whale-collector/worker.test.js
cd web
npm test -- src/v2/__tests__/BrowserCollection.test.tsx
npm run build
```

These checks use synthetic PDFs/mocked metadata. Actual extension registration,
Whale downloads, signed-in sites, installed Tauri and scientific review are
separate acceptance claims. Current checkpoint results are in
[open-source readiness](OPEN_SOURCE_READINESS.md).

## Official interfaces consulted

- [Whale extension compatibility and API table](https://developers.whale.naver.com/api/extensions/)
  points its downloads/runtime entries to Chrome documentation; differences still
  require actual browser checks.
- [Whale extension anatomy](https://developers.whale.naver.com/getting_started/anatomy_1/)
  and [Manifest V3 migration](https://developers.whale.naver.com/tutorials/mv3-migration/)
  document Whale extension URLs; do not assume Chrome's origin scheme.
- [Downloads API](https://developer.chrome.com/docs/extensions/reference/api/downloads)
  defines per-browser download IDs/completion and uses site cookies internally.
- [Native Messaging](https://developer.chrome.com/docs/extensions/develop/concepts/native-messaging)
  defines allowed origins and length-prefixed stdio; it is not tab isolation.
- [Edge Native Messaging](https://learn.microsoft.com/en-us/microsoft-edge/extensions/developer-guide/native-messaging)
  documents Edge's separate Windows registration path.
- [arXiv API manual](https://info.arxiv.org/help/api/user-manual.html)
  defines metadata queries and versioned identifiers. Respect source terms and
  access limits; neither collection nor source publication redistributes PDFs.
