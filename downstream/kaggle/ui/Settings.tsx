import {useEffect,useState} from "react";
import {useQuery,queryOptions} from "@tanstack/react-query";
import {getKaggleSettings,saveKaggleSettings,kagglePreflight,getSessions,closeSession,recoverCookie,type KaggleConfig} from "./api";
import {queryClient,workspaceKey,workspaceScope,isCurrentScope,setScopedQueryData,Button} from "../../bridge/ui";

export function KaggleSection(){
  const options=queryOptions({queryKey:workspaceKey("getKaggleSettings"),queryFn:({signal})=>getKaggleSettings(signal)});
  const query=useQuery(options);
  const sessions=useQuery({queryKey:workspaceKey("kaggleSessions"),queryFn:({signal})=>getSessions(signal),enabled:query.data?.runtimeReady===true,refetchInterval:5000});
  const [draft,setDraft]=useState<KaggleConfig|null>(null);
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState<string|null>(null);
  const [status,setStatus]=useState<string|null>(null);
  const [readiness,setReadiness]=useState<Record<string,string>>({});
  useEffect(()=>{if(query.data&&draft===null)setDraft(query.data.settings)},[query.data,draft]);
  if(!draft)return <p>{query.error?.message??"Loading Kaggle settings…"}</p>;
  const data=query.data;
  const set=<K extends keyof KaggleConfig>(key:K,value:KaggleConfig[K])=>{setDraft({...draft,[key]:value});setStatus(null)};
  async function act(task:()=>Promise<void>){
    if(busy)return;
    const scope=workspaceScope();setBusy(true);setError(null);setStatus(null);
    try{await task()}catch(e){if(isCurrentScope(scope))setError(e instanceof Error?e.message:String(e))}
    finally{if(isCurrentScope(scope))setBusy(false)}
  }
  async function save(){
    if(!draft)return;
    const scope=workspaceScope();
    const result=await saveKaggleSettings({...draft,competitionSources:draft.competitionSources.map(s=>s.trim()).filter(Boolean),datasetSources:draft.datasetSources.map(s=>s.trim()).filter(Boolean)});
    if(!isCurrentScope(scope))return;
    setScopedQueryData(options.queryKey,result);setStatus("Kaggle settings saved.");
    await queryClient.invalidateQueries({queryKey:workspaceKey("getComputeSettings")});
  }
  async function check(){
    if(!draft)return;
    const scope=workspaceScope();const result=await kagglePreflight(draft);
    if(isCurrentScope(scope))setReadiness(Object.fromEntries(result.accounts.map(a=>[a.account,a.readiness])));
  }
  const live=sessions.data?.sessions.filter(s=>s.state!=="stopped")??[];
  return <div className="flex flex-col gap-6">
    <form className="settings-form flex flex-col gap-4" onSubmit={e=>{e.preventDefault();void act(save)}}>
      <p className="m-0 text-sm text-subtext leading-relaxed">Each experiment Run leases a Kaggle notebook. Runs can reuse its environment until the session expires or you close it. Parallel Runs use different accounts; excess Runs wait for a free session.</p>
      {!data?.runtimeReady&&<p className="error" role="alert">Install the standalone runtime with downstream/kaggle/setup.ps1. Python, dependencies and account profiles stay in OpenResearch storage.</p>}
      <fieldset className="flex flex-col gap-3 rounded-lg border border-border p-4">
        <legend className="px-1 text-sm font-medium">Account pool</legend>
        <p className="m-0 text-xs text-subtext">Enable accounts available to all projects. Up to two GPU notebooks, or one CPU or TPU notebook, per account. No project is pinned to an account.</p>
        {data?.accounts.map(a=><div className="flex flex-wrap items-center gap-2" key={a.id}>
          <label className="flex flex-1 items-center gap-2 text-sm"><input type="checkbox" checked={draft.accounts.includes(a.id)} disabled={busy||!a.configured} onChange={e=>set("accounts",e.target.checked?[...draft.accounts,a.id]:draft.accounts.filter(id=>id!==a.id))}/>{a.username??a.alias}</label>
          <span className="text-xs text-subtext">{!a.configured?"Missing token":readiness[a.id]??"Not checked"}</span>
          <Button type="button" disabled={busy} onClick={()=>void act(async()=>{const scope=workspaceScope();const r=await recoverCookie(a.id);if(isCurrentScope(scope))setStatus(r.note??"Cookie recovery finished. Check accounts again.")})}>Recover cookie</Button>
        </div>)}
      </fieldset>
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <label className="flex flex-col gap-1 text-sm">Default accelerator<select value={draft.accelerator} disabled={busy} onChange={e=>set("accelerator",e.target.value)}>{(data?.accelerators??["cpu"]).map(a=><option key={a} value={a}>{a==="cpu"?"CPU":a==="NvidiaTeslaT4"?"GPU · T4 × 2":a}</option>)}</select></label>
        <label className="flex flex-col gap-1 text-sm">Notebook lifetime (minutes)<input type="number" min={1} max={720} required value={draft.ttlMinutes} disabled={busy} onChange={e=>set("ttlMinutes",Number(e.target.value))}/></label>
      </div>
      <p className="m-0 text-xs text-subtext">Lifetime starts when the notebook is reserved. Reusing it does not reset the deadline. Stopping a Run keeps its notebook available; Close session shuts down the notebook.</p>
      <label className="flex flex-col gap-1 text-sm">Competition sources<textarea rows={2} value={draft.competitionSources.join("\n")} disabled={busy} onChange={e=>set("competitionSources",e.target.value.split(/\r?\n/))}/></label>
      <label className="flex flex-col gap-1 text-sm">Dataset sources<textarea rows={2} value={draft.datasetSources.join("\n")} disabled={busy} onChange={e=>set("datasetSources",e.target.value.split(/\r?\n/))}/></label>
      <div className="flex flex-wrap gap-2"><Button type="submit" variant="primary" disabled={busy||!data?.runtimeReady||!draft.accounts.length}>{busy?"Working…":"Save Kaggle settings"}</Button><Button type="button" disabled={busy||!draft.accounts.length} onClick={()=>void act(check)}>Check accounts</Button></div>
      {(error??data?.error)&&<p className="error" role="alert">{error??data?.error}</p>}
      {status&&<p className="text-sm text-subtext" role="status">{status}</p>}
    </form>
    <section className="flex flex-col gap-3" aria-label="Kaggle sessions">
      <h3 className="m-0 text-sm font-medium">Kaggle sessions · {live.length} active</h3>
      {sessions.error&&<p className="error" role="alert">{sessions.error.message}</p>}
      {!live.length&&<p className="m-0 text-sm text-subtext">No live notebooks. A session is allocated when the next Run starts.</p>}
      {live.map(s=><article key={s.id} className="flex flex-col gap-2 rounded-lg border border-border p-3 text-sm">
        <div className="flex flex-wrap items-center justify-between gap-2"><strong>{data?.accounts.find(a=>a.id===s.account)?.username??s.account}</strong><span>{s.state}</span></div>
        <span className="text-xs text-subtext">Session {s.id.slice(0,12)} · expires {new Date(s.deadline*1000).toLocaleTimeString()}</span>
        {s.active_run&&<span className="text-xs text-subtext">Run {s.active_run.slice(0,12)}</span>}
        <Button type="button" disabled={busy||s.state==="closing"} onClick={()=>void act(async()=>{await closeSession(s.id);await sessions.refetch()})}>Close session</Button>
      </article>)}
    </section>
  </div>;
}
