import { useEffect, useState } from "react";
import { useQuery, queryOptions } from "@tanstack/react-query";
import { getKaggleSettings, discoverKaggle, saveKaggleSettings, kagglePreflight, type KaggleConfig } from "../api";
import { queryClient, workspaceKey, workspaceScope, isCurrentScope, setScopedQueryData } from "../queries/client";
import { m } from "../paraglide/messages.js";
import { Button } from "./ui/Button";

export function KaggleSection() {
  const options = queryOptions({ queryKey: workspaceKey("getKaggleSettings"), queryFn: ({ signal }) => getKaggleSettings(signal) });
  const query = useQuery(options);
  const [draft, setDraft] = useState<KaggleConfig | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  useEffect(() => { if (query.data && draft === null) setDraft(query.data.settings); }, [query.data, draft]);
  const data = query.data;
  if (!draft) return <p>{query.error?.message ?? m.common_loading()}</p>;
  const set = <K extends keyof KaggleConfig>(key: K, value: KaggleConfig[K]) => {
    setDraft({ ...draft, [key]: value }); setStatus(null);
  };
  async function act(action: "discover" | "save" | "check") {
    if (!draft || busy) return;
    const scope = workspaceScope();
    const body = { ...draft, competitionSources: draft.competitionSources.map(s => s.trim()).filter(Boolean), datasetSources: draft.datasetSources.map(s => s.trim()).filter(Boolean) };
    setBusy(true); setError(null); setStatus(null);
    try {
      if (action === "check") {
        const result = await kagglePreflight(body);
        if (isCurrentScope(scope)) setStatus(result.ready ? m.kaggle_ready() : m.kaggle_not_ready({ state: result.readiness, count: String(result.activeSessions ?? "?") }));
      } else {
        const result = await (action === "save" ? saveKaggleSettings(body) : discoverKaggle(body));
        if (!isCurrentScope(scope)) return;
        setScopedQueryData(options.queryKey, result);
        if (result.error) setError(result.error);
        else if (action === "save") {
          setStatus(m.settings_saved());
          await queryClient.invalidateQueries({ queryKey: workspaceKey("getComputeSettings") });
        }
      }
    } catch (err) { if (isCurrentScope(scope)) setError(err instanceof Error ? err.message : String(err)); }
    finally { if (isCurrentScope(scope)) setBusy(false); }
  }
  const lines = (value: string) => value.split(/\r?\n/);
  return <form className="settings-form flex flex-col gap-4" onSubmit={e => { e.preventDefault(); void act("save"); }}>
    <p className="m-0 text-subtext text-sm leading-relaxed">{m.kaggle_setup_note()}</p>
    <details open={!draft.python || !draft.donorRoot}>
      <summary className="cursor-pointer text-sm font-medium">{m.kaggle_runtime()}</summary>
      <div className="mt-3 flex flex-col gap-3">
        <label className="flex flex-col gap-1 text-sm">{m.kaggle_python()}<input value={draft.python} disabled={busy} onChange={e => set("python", e.target.value)} /></label>
        <label className="flex flex-col gap-1 text-sm">{m.kaggle_package()}<input value={draft.donorRoot} disabled={busy} onChange={e => set("donorRoot", e.target.value)} /></label>
        <Button type="button" disabled={busy || !draft.python || !draft.donorRoot} onClick={() => void act("discover")}>{m.kaggle_load_accounts()}</Button>
      </div>
    </details>
    <label className="flex flex-col gap-1 text-sm">{m.kaggle_account()}
      <select value={draft.account} disabled={busy} onChange={e => set("account", e.target.value)} required>
        <option value="">{m.kaggle_select_account()}</option>
        {(data?.accounts ?? []).map(a => <option key={a.id} value={a.id} disabled={!a.configured}>{a.username ?? a.alias} · {a.alias}{a.configured ? "" : ` (${m.kaggle_missing_token()})`}</option>)}
      </select>
    </label>
    <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
      <label className="flex flex-col gap-1 text-sm">{m.kaggle_accelerator()}<select value={draft.accelerator} disabled={busy} onChange={e => set("accelerator", e.target.value)}>
        {(data?.accelerators ?? ["cpu"]).map(a => <option key={a} value={a}>{a === "cpu" ? "CPU" : a === "NvidiaTeslaT4" ? "GPU · T4 × 2" : a}</option>)}
      </select></label>
      <label className="flex flex-col gap-1 text-sm">{m.kaggle_lifetime()}<input type="number" min={1} max={720} required value={draft.ttlMinutes} disabled={busy} onChange={e => set("ttlMinutes", Number(e.target.value))} /></label>
    </div>
    <p className="m-0 text-xs text-subtext leading-relaxed">{m.kaggle_lifetime_note()}</p>
    <label className="flex flex-col gap-1 text-sm">{m.kaggle_competitions()}<textarea rows={2} value={draft.competitionSources.join("\n")} disabled={busy} onChange={e => set("competitionSources", lines(e.target.value))} /></label>
    <label className="flex flex-col gap-1 text-sm">{m.kaggle_datasets()}<textarea rows={2} value={draft.datasetSources.join("\n")} disabled={busy} onChange={e => set("datasetSources", lines(e.target.value))} /></label>
    <p className="m-0 text-xs text-subtext">{m.kaggle_sources_note()}</p>
    <div className="flex flex-wrap gap-2">
      <Button type="submit" variant="primary" disabled={busy || !draft.account}>{busy ? m.common_loading() : m.kaggle_save()}</Button>
      <Button type="button" disabled={busy || !draft.account} onClick={() => void act("check")}>{m.kaggle_check()}</Button>
    </div>
    {(error ?? data?.error) && <p className="error" role="alert">{error ?? data?.error}</p>}
    {status && <p className="text-sm text-subtext" role="status">{status}</p>}
  </form>;
}
