import type { components } from "./schema";

type S = components["schemas"];

export type Agent = S["Agent"];
export type AgentInput = S["AgentInput"];
export type Approval = S["Approval"];
export type Assignment = S["Assignment"];
export type ArtifactView = S["ArtifactView"];
export type CollectionView = S["CollectionView"];
export type CreatedToken = S["CreatedToken"];
export type DelegationLimits = S["DelegationLimits"];
export type DelegationOverrides = S["DelegationOverrides"];
export type DisagreementView = S["DisagreementView"];
export type DeploymentView = S["DeploymentView"];
export type EventView = S["EventView"];
export type Health = S["Health"];
export type IngestResultView = S["IngestResultView"];
export type LineageStep = S["LineageStep"];
export type McpServerView = S["McpServerView"];
export type MemoryDetail = S["MemoryDetail"];
export type MemoryView = S["MemoryView"];
export type OrchestrationNode = S["OrchestrationNode"];
export type OrchestrationView = S["OrchestrationView"];
export type ProbeResult = S["ProbeResult"];
export type PrincipalView = S["PrincipalView"];
export type RefusalView = S["RefusalView"];
export type ReportView = S["ReportView"];
export type RetrievedPassage = S["RetrievedPassage"];
export type RunDetail = S["RunDetail"];
export type RunPhase = S["RunPhase"];
export type RunSummary = S["RunSummary"];
export type RunPage = S["Page_RunSummary_"];
export type SearchResultView = S["SearchResultView"];
export type Session = S["Session"];
export type SettingsView = S["SettingsView"];
export type SourceDetail = S["SourceDetail"];
export type SourceView = S["SourceView"];
export type TokenView = S["TokenView"];
export type ToolActivity = S["ToolActivity"];
export type ToolCallView = S["ToolCallView"];
export type ToolView = S["ToolView"];
export type TurnView = S["TurnView"];
export type WriteResultView = S["WriteResultView"];

export const PHASES: RunPhase[] = [
  "running",
  "waiting_approval",
  "queued",
  "completed",
  "failed",
  "cancelled",
  "interrupted",
];

export const ACTIVE: RunPhase[] = ["running", "queued"];

export const TERMINAL: RunPhase[] = ["completed", "failed", "cancelled"];
