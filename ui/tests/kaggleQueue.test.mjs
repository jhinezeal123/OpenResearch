import assert from "node:assert/strict";
import test from "node:test";
import { act, createElement } from "react";
import { KaggleSection } from "../../downstream/kaggle/ui/Settings.tsx";
import { RunContactNotice } from "../src/components/RunContactNotice.tsx";
import { ExperimentRunBanner } from "../src/components/ExperimentRunBanner.tsx";
import { StatusBadge } from "../src/components/StatusBadge.tsx";
import { runDisplay, runNotice } from "../src/runDisplay.ts";
import { m } from "../src/paraglide/messages.js";
import { render, serve, settle } from "./render.mjs";

test("queue labels and accessible notices give way to contact failure, cancellation and running", async () => {
  serve(path => path === "/api/compute/hosts" ? [] : undefined);
  const queued = { id: "run", status: "starting", contact: null, cancelling: false, host: null, backend: null, queueDetail: "Queued on Kaggle" };
  const { container, rerender } = await render(createElement(RunContactNotice, { run: queued }));
  await settle();
  assert.equal(container.querySelector('[role="status"]').textContent, queued.queueDetail);
  const badge = await render(createElement(StatusBadge, runDisplay(queued)));
  assert.ok(badge.container.textContent.includes(m.status_queued()));
  const banner = await render(createElement(ExperimentRunBanner, { runs: [{ run: { ...queued, createdAt: 1 }, experimentName: "Queue test" }] }));
  assert.ok(banner.container.textContent.includes(m.status_queued()));
  assert.ok(banner.container.textContent.includes(queued.queueDetail));
  const unknown = { ...queued, contact: { type: "unreachable", since: 1, cause: "Could not check Kaggle status" } };
  assert.equal(runDisplay(unknown).status, "unreachable");
  await rerender(createElement(RunContactNotice, { run: unknown }));
  assert.ok(container.querySelector('[role="status"]').textContent.includes(unknown.contact.cause));
  assert.ok(!container.textContent.includes(queued.queueDetail));
  assert.equal(runDisplay({ ...queued, cancelling: true }).status, "cancelling");
  assert.equal(runDisplay({ ...queued, status: "running" }).status, "running");
  assert.equal(runNotice({ ...queued, status: "running" }), null);
  assert.equal(runNotice({ ...queued, status: "done" }), null);
});

test("Kaggle sessions show provider queue, SSH wait and failed checks as separate states", async () => {
  let session = { id: "s", account: "qa", created: 1, deadline: 100, state: "provisioning", active_run: "r", note: null, provider_status: "queued", detail: "Queued on Kaggle" };
  serve(path => {
    if (path === "/api/settings/kaggle") return { settings: { accounts: ["qa"], accelerator: "cpu", ttlMinutes: 60, competitionSources: [], datasetSources: [] }, accounts: [{ id: "qa", alias: "QA", username: "qa", configured: true }], accelerators: ["cpu"], configured: true, runtimeReady: true, error: null };
    if (path === "/api/settings/kaggle/quota") return { accounts: [] };
    if (path === "/api/settings/kaggle/sessions") return { sessions: [session] };
  });
  const { container, workspace } = await render(createElement(KaggleSection));
  await settle();
  assert.equal(container.querySelector('article [role="status"]').textContent, session.detail);
  assert.ok(container.querySelector("article").textContent.includes(m.status_queued()));
  session = { ...session, provider_status: "running", detail: "Waiting for SSH" };
  await act(async () => workspace.queryClient.invalidateQueries({ queryKey: workspace.workspaceKey("kaggleSessions") }));
  await settle();
  assert.equal(container.querySelector('article [role="status"]').textContent, session.detail);
  assert.ok(!container.querySelector("article").textContent.includes(m.status_queued()));
  session = { ...session, error: "Could not check the Kaggle notebook status" };
  await act(async () => workspace.queryClient.invalidateQueries({ queryKey: workspace.workspaceKey("kaggleSessions") }));
  await settle();
  assert.equal(container.querySelector('article [role="status"]').textContent, session.error);
  assert.ok(container.querySelector("article").textContent.includes(m.status_unconfirmed()));
});
