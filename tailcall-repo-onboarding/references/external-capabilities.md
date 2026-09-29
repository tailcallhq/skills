# Desktop GUI, mobile, container-only, cloud

These need capabilities the agent may not have: a display, an emulator/device
(`adb`, `xcrun simctl`), a container runtime (`docker`, `podman`), cloud
credentials. Probe with `command -v` and a harmless status call
(`docker info`, `adb devices`) — presence of a binary is not proof it works.

- Missing capability → verdict **blocked**, naming it. Never report pass on
  what you could not run.
- Still do what can run headless: unit tests, build/compile, lint.
- Never deploy, push images, or touch cloud resources as setup.
- Hand interactive/visual verification to the user with the exact command.
