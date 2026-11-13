import type {Workspace} from "../../bridge/ui";
export interface KaggleConfig {
  accounts:string[];
  accelerator:string;
  ttlMinutes:number;
  competitionSources:string[];
  datasetSources:string[];
}
export interface KaggleSettings {
  settings:KaggleConfig;
  accounts:{id:string;alias:string;username:string|null;configured:boolean}[];
  accelerators:string[];
  configured:boolean;
  runtimeReady:boolean;
  error:string|null;
}
export interface KaggleSession {id:string;account:string;created:number;deadline:number;state:string;active_run:string|null;note:string|null;provider_status?:string|null;detail?:string;error?:string}
export interface KaggleQuotaHours {used:number;reserved:number;total:number;remaining:number}
export interface KaggleAccountQuota {
  account:string;
  gpu?:KaggleQuotaHours|null;
  tpu?:KaggleQuotaHours|null;
  refresh_at?:string|null;
  error?:string;
  blocks:{quota:string;until:number;reason:string|null}[];
}
/** The Kaggle settings endpoints, scoped to one workspace's server. */
export function kaggleApi({get,post}:Workspace["api"]){
  return {
    getKaggleSettings:(signal?:AbortSignal)=>get<KaggleSettings>("/api/settings/kaggle",signal),
    discoverKaggle:(body:KaggleConfig)=>post<KaggleSettings>("/api/settings/kaggle/discover",body),
    saveKaggleSettings:(body:KaggleConfig)=>post<KaggleSettings>("/api/settings/kaggle",body),
    kagglePreflight:(body:KaggleConfig)=>post<{accounts:{account:string;readiness:string;active_session_count?:number}[]}>("/api/settings/kaggle/preflight",body),
    getSessions:(signal?:AbortSignal)=>get<{sessions:KaggleSession[]}>("/api/settings/kaggle/sessions",signal),
    getQuota:(signal?:AbortSignal)=>get<{accounts:KaggleAccountQuota[]}>("/api/settings/kaggle/quota",signal),
    closeSession:(sessionId:string)=>post<{closing:boolean}>("/api/settings/kaggle/close-session",{sessionId}),
    recoverCookie:(account:string)=>post<{stage?:string;note?:string}>("/api/settings/kaggle/recover-cookie",{account}),
  };
}
