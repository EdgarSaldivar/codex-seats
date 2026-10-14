# Trusted development acceptance inputs

These are small sample inputs for the four managed-worker passes in
[the worker guide](../../../docs/trusted-development-workers.md). They do not add
agent tools or prescribe a tool catalog. Use existing installed tools.

## Native

`native/` is a SwiftUI app scaffolded with XcodeBuildMCP 2.7.0, with one label,
tap counter and button. The Swift package has one real test that creates an
IOSurface-backed pixel buffer. Generated editor instructions were omitted so
the fixture does not prescribe tool choice.

Run `swift test --package-path native/TrustedDevelopmentDemoPackage` and build
the `TrustedDevelopmentDemo` scheme in `native/TrustedDevelopmentDemo.xcworkspace`
directly for iOS Simulator. Use ordinary toolchain sandboxing; do not disable
macro validation or the SwiftPM sandbox. Simulator signing may be disabled.

Build/install/launch on the controller-assigned test simulator UDID. View a
screenshot, press **Add one** and confirm **Tap count: 1**. Change the orange
**Development demo** heading to blue **Updated demo**, rebuild/relaunch and view
the changed result. The button must still work. Preserve before/after images.

## Web

`web/index.html` is a dependency-free page with a button and deliberate fixed-width
layout defect. Serve it locally, view screenshots at desktop and narrow widths,
press **Add one**, and confirm the count changes. Fix the visible overflow,
reload, and view both widths again. Use an isolated browser profile/session.

## Evidence and cleanup

Use separate disposable checkouts for Codex/native, Claude/native, Codex/web and
Claude/web. Retain commands, real viewed images, interaction results, provider
and tool versions, and the worker's effective access mode in the normal receipt.
Also prove inherited skill resources and an already-configured MCP call as
required by the test task. Head-only runs establish fixture readiness, not acceptance.

Keep screenshots and a concise command/result record in a private, task-owned
location outside the public repository. Report sanitized results in the receipt.
The head reviews each patch and its images without applying fixture edits between
passes, so all four jobs start from the same fixture revision.

Stop only the test-owned app/server/browser session and assigned simulator.
No production connection, real credentials, global setup or new service is needed.
