import { messages } from "./kaggle/ui/messages";
export type ComputeId = "kaggle";
export type HarnessId = "dsh";
export const computeLabels = {kaggle: () => "Kaggle SSH"};
export const computeDescriptions = {kaggle: () => messages.kaggle_description};
export const computeKinds = {kaggle: "kaggle_job"};
export const flavoredTargets: ComputeId[] = ["kaggle"];
export const flavorSuggestions = {kaggle: ["cpu", "NvidiaTeslaT4", "TpuV5E8", "TpuV6E8"]};
export const harnessLabels = {dsh: "DSH"};
