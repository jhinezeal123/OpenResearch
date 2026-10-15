//! Build-time UI registrations for downstream plugins.
import { Server } from "lucide-react";
import { KaggleSection } from "./kaggle/ui/Settings";
import { DshLogo } from "./dsh/ui";
export * from "./catalog";
export function Settings({id}: {id:string}) { return id === "kaggle" ? <KaggleSection/> : null; }
export function harnessLogo(id:string, size:number) { return id === "dsh" ? <DshLogo size={size}/> : null; }
export function backendLogo(kind:string, size:number) { return kind === "kaggle_job" ? <Server size={size} strokeWidth={1.5}/> : null; }
export function backendLabel(kind:string) { return kind === "kaggle_job" ? "Kaggle SSH" : null; }
