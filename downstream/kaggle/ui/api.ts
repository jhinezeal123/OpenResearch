import { get, post } from "../../bridge/ui";
export interface KaggleConfig {
  python: string;
  donorRoot: string;
  account: string;
  accelerator: string;
  ttlMinutes: number;
  competitionSources: string[];
  datasetSources: string[];
}
export interface KaggleSettings {
  settings: KaggleConfig;
  accounts: { id: string; alias: string; username: string | null; configured: boolean }[];
  accelerators: string[];
  configured: boolean;
  error: string | null;
}
export const getKaggleSettings = (signal?: AbortSignal) => get<KaggleSettings>("/api/settings/kaggle", signal);
export const discoverKaggle = (body: KaggleConfig) => post<KaggleSettings>("/api/settings/kaggle/discover", body);
export const saveKaggleSettings = (body: KaggleConfig) => post<KaggleSettings>("/api/settings/kaggle", body);
export const kagglePreflight = (body: KaggleConfig) => post<{ ready: boolean; readiness: string; activeSessions: number | null }>("/api/settings/kaggle/preflight", body);

