/** Data hooks over the REST API (TanStack Query). The server computes every view; these only
 * fetch, cache and invalidate. */

import { useMutation, useQuery, useQueryClient, type QueryKey } from "@tanstack/react-query";
import { useEffect, useRef } from "react";
import { api, seg, segs } from "./client";
import type {
  Agent,
  Approval,
  ContractView,
  PolicyOverlayRule,
  SkillsView,
  TemplateDetail,
  TemplateExport,
  TemplateView,
  UpgradePreview,
  ArtifactView,
  CollectionView,
  CreatedToken,
  DeploymentView,
  EventView,
  Health,
  IngestResultView,
  McpServerView,
  MemoryDetail,
  MemoryView,
  OrchestrationView,
  PrincipalView,
  ProbeResult,
  DelegationOverrides,
  RunDetail,
  RunPage,
  RunPhase,
  RunSummary,
  SearchResultView,
  Session,
  SettingsView,
  SourceDetail,
  SourceView,
  TokenView,
  ToolActivity,
  ToolView,
  WriteResultView,
} from "./types";

export const keys = {
  session: ["session"] as const,
  health: ["health"] as const,
  runs: (params: object) => ["runs", params] as const,
  run: (id: string) => ["run", id] as const,
  tree: (id: string) => ["tree", id] as const,
  approvals: ["approvals"] as const,
  agents: ["agents"] as const,
  agent: (name: string) => ["agent", name] as const,
  templates: ["templates"] as const,
  template: (id: string) => ["template", id] as const,
  contracts: ["contracts"] as const,
  skills: ["skills"] as const,
  upgrade: (name: string) => ["upgrade", name] as const,
  deployments: ["deployments"] as const,
  tools: ["tools"] as const,
  activity: ["activity"] as const,
  mcp: ["mcp"] as const,
  collections: ["collections"] as const,
  sources: (collection: string) => ["sources", collection] as const,
  source: (collection: string, id: string) => ["source", collection, id] as const,
  memories: (filters: object) => ["memories", filters] as const,
  memory: (id: string) => ["memory", id] as const,
  scopes: ["scopes"] as const,
  tokens: ["tokens"] as const,
  principals: ["principals"] as const,
  settings: ["settings"] as const,
  audit: ["audit"] as const,
};

function useInvalidate() {
  const client = useQueryClient();
  return (...prefixes: QueryKey[]) =>
    Promise.all(prefixes.map((queryKey) => client.invalidateQueries({ queryKey })));
}

// --- identity and system -------------------------------------------------------------------------

export const useSession = () =>
  useQuery({ queryKey: keys.session, queryFn: () => api.get<Session>("/v1/session"), retry: false, staleTime: 60_000 });

export function useSignIn() {
  const client = useQueryClient();
  return useMutation({
    mutationKey: ["sign-in"],
    mutationFn: (token: string) => api.post<Session>("/v1/session", { token }),
    onSuccess: (session) => {
      client.clear();
      client.setQueryData(keys.session, session);
    },
  });
}

export function useSignOut() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () => api.delete("/v1/session"),
    onSettled: () => client.clear(),
  });
}

export const useHealth = () =>
  useQuery({ queryKey: keys.health, queryFn: () => api.get<Health>("/v1/health"), refetchInterval: 15_000 });

export const useSettings = () =>
  useQuery({ queryKey: keys.settings, queryFn: () => api.get<SettingsView>("/v1/settings") });

export function useUpdateConfig() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: (changes: Record<string, unknown>) => api.patch<SettingsView>("/v1/settings/config", changes),
    onSuccess: () => invalidate(["settings"], ["health"], ["deployments"], ["tools"], ["agents"]),
  });
}

export const useAudit = (enabled: boolean) =>
  useQuery({
    queryKey: keys.audit,
    queryFn: () => api.get<Record<string, string>[]>("/v1/audit", { limit: 200 }),
    enabled,
  });

// --- runs -----------------------------------------------------------------------------------------

export interface RunFilters {
  phase?: RunPhase | undefined;
  agent?: string | undefined;
  /** Leave out runs another run delegated. */
  roots?: boolean | undefined;
  limit?: number;
  offset?: number;
}

const busy = (phases: RunPhase[]) => phases.some((p) => p === "running" || p === "queued");

export const useRuns = (filters: RunFilters = {}) =>
  useQuery({
    queryKey: keys.runs(filters),
    queryFn: () => api.get<RunPage>("/v1/runs", { limit: 25, ...filters }),
    refetchInterval: (query) => (busy(query.state.data?.items.map((r) => r.phase) ?? []) ? 2_000 : 10_000),
    placeholderData: (previous) => previous,
  });

export const useRun = (id: string) =>
  useQuery({
    queryKey: keys.run(id),
    queryFn: () => api.get<RunDetail>(`/v1/runs/${seg(id)}`),
    refetchInterval: (query) => (busy(query.state.data ? [query.state.data.summary.phase] : []) ? 3_000 : false),
  });

/** The orchestration a run belongs to: its root and every run delegated below it that the caller
 * can see. Polls while any run in it is still going. */
export const useRunTree = (id: string, enabled = true) =>
  useQuery({
    queryKey: keys.tree(id),
    queryFn: () => api.get<OrchestrationView>(`/v1/runs/${seg(id)}/tree`),
    enabled,
    refetchInterval: (query) =>
      query.state.data?.nodes.some((n) => !["completed", "failed", "cancelled"].includes(n.run.phase)) ? 3_000 : false,
  });

export interface StartRunInput {
  input: string;
  agent?: string | undefined;
  session?: string | undefined;
}

export function useStartRun() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: (spec: StartRunInput) => api.post<RunSummary>("/v1/runs", spec),
    onSuccess: () => invalidate(["runs"], ["health"]),
  });
}

export function useRunAction() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: ({ id, action, reason }: { id: string; action: "cancel" | "resume"; reason?: string }) =>
      api.post<RunSummary>(`/v1/runs/${seg(id)}/${action}`, action === "cancel" ? { reason: reason ?? "" } : undefined),
    onSuccess: (_summary, { id }) => invalidate(keys.run(id), ["tree"], ["runs"], ["approvals"], ["health"]),
  });
}

/** Live events of a run (Server-Sent Events, resumed automatically by the browser). */
export function useRunStream(id: string, enabled: boolean, onEvent: (event: EventView) => void) {
  const callback = useRef(onEvent);
  useEffect(() => {
    callback.current = onEvent;
  });
  const client = useQueryClient();
  useEffect(() => {
    if (!enabled || typeof EventSource === "undefined") return;
    const source = new EventSource(`/v1/runs/${seg(id)}/stream`);
    let timer: number | undefined;
    const refresh = () => {
      window.clearTimeout(timer);
      timer = window.setTimeout(() => {
        void client.invalidateQueries({ queryKey: keys.run(id) });
        void client.invalidateQueries({ queryKey: ["tree"] });
        void client.invalidateQueries({ queryKey: ["events", id] });
        void client.invalidateQueries({ queryKey: keys.approvals });
      }, 150);
    };
    source.addEventListener("run_event", (message) => {
      callback.current(JSON.parse((message as MessageEvent<string>).data) as EventView);
      refresh();
    });
    source.addEventListener("end", () => {
      source.close();
      refresh();
      void client.invalidateQueries({ queryKey: ["runs"] });
    });
    return () => {
      window.clearTimeout(timer);
      source.close();
    };
  }, [id, enabled, client]);
}

export const useRunEvents = (id: string) =>
  useQuery({ queryKey: ["events", id], queryFn: () => api.get<EventView[]>(`/v1/runs/${seg(id)}/events`) });

export const useArtifact = (runId: string, artifactId: string | null) =>
  useQuery({
    queryKey: ["artifact", runId, artifactId],
    queryFn: () => api.get<ArtifactView>(`/v1/runs/${seg(runId)}/artifacts/${seg(artifactId ?? "")}`),
    enabled: artifactId !== null,
  });

// --- approvals -------------------------------------------------------------------------------------

export const useApprovals = () =>
  useQuery({ queryKey: keys.approvals, queryFn: () => api.get<Approval[]>("/v1/approvals"), refetchInterval: 4_000 });

export function useDecide() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: ({ approval, approved, reason }: { approval: Approval; approved: boolean; reason?: string }) =>
      api.post<RunSummary>(`/v1/runs/${seg(approval.run_id)}/approvals/${segs(approval.invocation_id)}`, {
        approved,
        reason: reason || null,
      }),
    onSuccess: (_summary, { approval }) =>
      invalidate(keys.approvals, keys.run(approval.run_id), ["tree"], ["runs"], ["health"], ["activity"]),
  });
}

// --- agents ----------------------------------------------------------------------------------------

export const useAgents = () => useQuery({ queryKey: keys.agents, queryFn: () => api.get<Agent[]>("/v1/agents") });

export const useAgent = (name: string | undefined) =>
  useQuery({
    queryKey: keys.agent(name ?? ""),
    queryFn: () => api.get<Agent>(`/v1/agents/${seg(name ?? "")}`),
    enabled: Boolean(name),
  });

export type AgentDraft = {
  name: string;
  description: string;
  instructions: string;
  model: string;
  tools: string[];
  memory: { read: string[]; write: string[] } | null;
  knowledge: { collections: string[]; labels: string[] } | null;
  budget: { max_output_tokens: number } | null;
  record_episodes: boolean;
  delegates: string[];
  /** Narrows the workspace's delegation limits (unset fields inherit); null keeps them. */
  delegation: DelegationOverrides | null;
  /** Overlay rules: they only make the workspace policy stricter for this agent's runs. */
  policy: PolicyOverlayRule[];
  contract: string | null;
  skills: string[];
};

export function useSaveAgent() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: ({ draft, existing }: { draft: AgentDraft; existing: boolean }) =>
      existing ? api.put<Agent>(`/v1/agents/${seg(draft.name)}`, draft) : api.post<Agent>("/v1/agents", draft),
    onSuccess: (agent) => invalidate(keys.agents, keys.agent(agent.name), keys.upgrade(agent.name), keys.templates),
  });
}

export function useDeleteAgent() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: (name: string) => api.delete(`/v1/agents/${seg(name)}`),
    onSuccess: () => invalidate(keys.agents),
  });
}

// --- specialization ----------------------------------------------------------------------------------

export const useTemplates = () =>
  useQuery({ queryKey: keys.templates, queryFn: () => api.get<TemplateView[]>("/v1/templates") });

export const useTemplate = (id: string) =>
  useQuery({ queryKey: keys.template(id), queryFn: () => api.get<TemplateDetail>(`/v1/templates/${seg(id)}`) });

export const useContracts = () =>
  useQuery({ queryKey: keys.contracts, queryFn: () => api.get<ContractView[]>("/v1/contracts") });

export const useSkills = () => useQuery({ queryKey: keys.skills, queryFn: () => api.get<SkillsView>("/v1/skills") });

export type FromTemplate = {
  template: string;
  name: string;
  model: string;
  parameters: Record<string, string>;
  overrides: {
    knowledge?: { collections: string[]; labels: string[] };
    delegates?: string[];
  };
};

export function useCreateFromTemplate() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: (spec: FromTemplate) => api.post<Agent>("/v1/agents/from-template", spec),
    onSuccess: () => invalidate(keys.agents, keys.templates),
  });
}

export const useUpgradePreview = (name: string, enabled: boolean) =>
  useQuery({
    queryKey: keys.upgrade(name),
    queryFn: () => api.get<UpgradePreview>(`/v1/agents/${seg(name)}/upgrade`),
    enabled,
  });

export function useUpgradeAgent() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: ({ name, digest, reset }: { name: string; digest: string; reset: string[] }) =>
      api.post<Agent>(`/v1/agents/${seg(name)}/upgrade`, { digest, reset }),
    onSuccess: (agent) => invalidate(keys.agents, keys.agent(agent.name), keys.upgrade(agent.name), keys.templates),
  });
}

export function useExportAgent() {
  return useMutation({
    mutationFn: ({ name, id }: { name: string; id?: string }) =>
      api.post<TemplateExport>(`/v1/agents/${seg(name)}/export`, id ? { id } : {}),
  });
}

export function useCreateStarter() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: () => api.post<Agent>("/v1/agents/starter"),
    onSuccess: () => invalidate(keys.agents, keys.collections),
  });
}

// --- models and tools -----------------------------------------------------------------------------

export const useDeployments = () =>
  useQuery({ queryKey: keys.deployments, queryFn: () => api.get<DeploymentView[]>("/v1/deployments") });

export function useSaveDeployment() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: (deployment: Record<string, unknown>) =>
      api.put<DeploymentView>(`/v1/deployments/${seg(String(deployment.id))}`, { deployment }),
    onSuccess: () => invalidate(keys.deployments, keys.health, keys.settings),
  });
}

export function useDeleteDeployment() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: (id: string) => api.delete(`/v1/deployments/${seg(id)}`),
    onSuccess: () => invalidate(keys.deployments, keys.health, keys.settings),
  });
}

export const useProbe = () =>
  useMutation({ mutationFn: (id: string) => api.post<ProbeResult>(`/v1/deployments/${seg(id)}/probe`) });

export const useTools = () => useQuery({ queryKey: keys.tools, queryFn: () => api.get<ToolView[]>("/v1/tools") });

export const useToolActivity = () =>
  useQuery({
    queryKey: keys.activity,
    queryFn: () => api.get<ToolActivity[]>("/v1/tools/activity", { limit: 30 }),
    refetchInterval: 10_000,
  });

export const useMcpServers = () =>
  useQuery({ queryKey: keys.mcp, queryFn: () => api.get<McpServerView[]>("/v1/mcp/servers") });

export function useConnectMcp() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: (name: string) => api.post<McpServerView>(`/v1/mcp/servers/${seg(name)}/connect`),
    onSuccess: () => invalidate(keys.mcp, keys.tools),
  });
}

// --- knowledge --------------------------------------------------------------------------------------

export const useCollections = () =>
  useQuery({ queryKey: keys.collections, queryFn: () => api.get<CollectionView[]>("/v1/knowledge/collections") });

export function useCreateCollection() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: (spec: { name: string; description: string }) =>
      api.post<CollectionView>("/v1/knowledge/collections", spec),
    onSuccess: () => invalidate(keys.collections),
  });
}

export function useDeleteCollection() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: (name: string) => api.delete(`/v1/knowledge/collections/${seg(name)}`),
    onSuccess: () => invalidate(keys.collections),
  });
}

export const useSources = (collection: string) =>
  useQuery({
    queryKey: keys.sources(collection),
    queryFn: () => api.get<SourceView[]>(`/v1/knowledge/collections/${seg(collection)}/sources`),
  });

export const useSource = (collection: string, id: string | null) =>
  useQuery({
    queryKey: keys.source(collection, id ?? ""),
    queryFn: () => api.get<SourceDetail>(`/v1/knowledge/collections/${seg(collection)}/sources/${seg(id ?? "")}`),
    enabled: id !== null,
  });

export function useUpload(collection: string) {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: ({ file, key, labels }: { file: File; key: string; labels: string[] }) =>
      api.upload<IngestResultView>(`/v1/knowledge/collections/${seg(collection)}/documents/${segs(key)}`, file, {
        labels,
        filename: file.name,
      }),
    onSuccess: () => invalidate(keys.sources(collection), keys.collections),
  });
}

export function useDocumentAction(collection: string) {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: async ({ key, labels }: { key: string; labels?: string[] }): Promise<SourceView | undefined> => {
      const path = `/v1/knowledge/collections/${seg(collection)}/documents/${segs(key)}`;
      if (labels !== undefined) return api.patch<SourceView>(path, { labels });
      await api.delete(path);
      return undefined;
    },
    onSuccess: () => invalidate(keys.sources(collection), keys.collections, ["source"]),
  });
}

export const useSearch = () =>
  useMutation({
    mutationFn: (spec: { query: string; collections: string[] }) =>
      api.post<SearchResultView>("/v1/knowledge/search", { ...spec, limit: 8 }),
  });

// --- memory -------------------------------------------------------------------------------------------

export interface MemoryFilters {
  scope?: string | undefined;
  kind?: string | undefined;
  status?: string | undefined;
  query?: string | undefined;
}

export const useMemories = (filters: MemoryFilters) =>
  useQuery({
    queryKey: keys.memories(filters),
    queryFn: () => api.get<MemoryView[]>("/v1/memory", { ...filters, limit: 300 }),
    placeholderData: (previous) => previous,
  });

export const useMemory = (id: string | null) =>
  useQuery({
    queryKey: keys.memory(id ?? ""),
    queryFn: () => api.get<MemoryDetail>(`/v1/memory/${seg(id ?? "")}`),
    enabled: id !== null,
  });

export const useScopes = () => useQuery({ queryKey: keys.scopes, queryFn: () => api.get<string[]>("/v1/memory/scopes") });

export function useRemember() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: (spec: { scope: string; content: string; kind: string; subject?: string; key?: string }) =>
      api.post<WriteResultView>("/v1/memory", spec),
    onSuccess: () => invalidate(["memories"], keys.scopes),
  });
}

export function useMemoryAction() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: ({ id, action, reason }: { id: string; action: "retract" | "forget"; reason: string }) =>
      api.post(`/v1/memory/${seg(id)}/${action}`, { reason }),
    onSuccess: () => invalidate(["memories"], ["memory"]),
  });
}

// --- tokens and principals --------------------------------------------------------------------------

export const useTokens = () => useQuery({ queryKey: keys.tokens, queryFn: () => api.get<TokenView[]>("/v1/tokens") });

export function useCreateToken() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: (spec: { principal: string; label: string; expires_in_days: number | null }) =>
      api.post<CreatedToken>("/v1/tokens", spec),
    onSuccess: () => invalidate(keys.tokens),
  });
}

export function useRevokeToken() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: (id: string) => api.delete(`/v1/tokens/${seg(id)}`),
    onSuccess: () => invalidate(keys.tokens),
  });
}

export const usePrincipals = (enabled: boolean) =>
  useQuery({ queryKey: keys.principals, queryFn: () => api.get<PrincipalView[]>("/v1/principals"), enabled });

export function useCreatePrincipal() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: (spec: Record<string, unknown>) => api.post<PrincipalView>("/v1/principals", spec),
    onSuccess: () => invalidate(keys.principals),
  });
}
