# Adapter: surfaces that need an external driver

Forge has no built-in browser, GUI, mobile-device or container driver. These
checks are possible only when the project or machine already provides one.
Probe; don't assume, and don't install global tooling or start paid/cloud
resources to get unblocked without the user's approval.

| Surface | Probe (read-only) | If present | If absent |
|---|---|---|---|
| Web UI | project has Playwright/Cypress/Selenium config and browsers installed (`npx playwright --version`, existing `test:e2e` script) | run the project's e2e command against a local server | test the HTTP layer (see `api.md`: page returns 200, expected markup/strings, API calls the page makes); mark in-browser behavior BLOCKED |
| Desktop GUI | display available (`DISPLAY`/`WAYLAND_DISPLAY`, or a desktop session on macOS/Windows) **and** a GUI test driver in the project | run it | unit/headless tests only; UI checks BLOCKED |
| Mobile | `adb devices` / `xcrun simctl list` shows a device or booted simulator | project's instrumented tests | build + unit tests; device checks BLOCKED |
| Containers | `docker info` / `podman info` succeeds | `compose up -d --wait`, test, `compose down -v` | run natively if the project supports it; otherwise BLOCKED |
| Data/ML | pinned small sample data and seeds in repo | run on the sample; compare metrics to a stated tolerance | BLOCKED if it needs real datasets, GPUs or paid APIs |
| Infra (Terraform, k8s, cloud) | local validators (`terraform validate`, `kubeconform`, kind cluster) | static validation + local cluster only | plan-only; applying to real accounts is out of scope without explicit approval |

## Visual evidence

A screenshot can show that something rendered. It cannot prove a click did
the right thing, data persisted, or an error path works. Pair it with an
assertion on state (DOM text, network response, stored record). Use image
reading to *describe* screenshots, and say what the screenshot does and does
not show.

## Reporting a blocker

Say exactly what was missing, the probe you ran and its output, what you
verified instead (and at what layer), and the command the user could run
themselves to complete the check.
