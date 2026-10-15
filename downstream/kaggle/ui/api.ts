import {get,post} from "../../bridge/ui";
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
export interface KaggleSession {id:string;account:string;created:number;deadline:number;state:string;active_run:string|null;note:string|null}
export const getKaggleSettings=(signal?:AbortSignal)=>get<KaggleSettings>("/api/settings/kaggle",signal);
export const discoverKaggle=(body:KaggleConfig)=>post<KaggleSettings>("/api/settings/kaggle/discover",body);
export const saveKaggleSettings=(body:KaggleConfig)=>post<KaggleSettings>("/api/settings/kaggle",body);
export const kagglePreflight=(body:KaggleConfig)=>post<{accounts:{account:string;readiness:string;active_session_count?:number}[]}>("/api/settings/kaggle/preflight",body);
export const getSessions=(signal?:AbortSignal)=>get<{sessions:KaggleSession[]}>("/api/settings/kaggle/sessions",signal);
export const closeSession=(sessionId:string)=>post<{closing:boolean}>("/api/settings/kaggle/close-session",{sessionId});
export const recoverCookie=(account:string)=>post<{stage?:string;note?:string}>("/api/settings/kaggle/recover-cookie",{account});
