# Downstream integrations

This directory owns custom integrations shipped in this fork. Kaggle compute
and DSH harness are independent plugins; neither imports the other.

- `bridge.rs` and `bridge/ui.ts` contain the compatibility boundary to upstream.
- `mod.rs` and `ui.tsx` register implementations, routes, metadata and settings.
- `kaggle/` owns compute, notebook lifecycle, native runtime, UI and tests.
- `dsh/` owns ACP model/effort discovery, harness lifecycle and setup.

Small upstream hooks call these registrations. Generated `ui/dist` remains in
its upstream release location. Updates must merge upstream and build this fork;
an official upstream binary does not contain these integrations.

Refactors preserve behavior before lifecycle/pool changes are applied in a
separate commit. Runtime profiles and secrets live in the application data
folder and must never be committed here.
