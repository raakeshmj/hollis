import { useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router";
import { ApiError } from "../api/client";
import { useAgents, useCollections, useContracts, useCreateFromTemplate, useTemplate } from "../api/hooks";
import type { PolicyOverlayRule, RequirementCheck, TemplateDetail as Detail } from "../api/types";
import { Empty, ErrorState, Field, FormError, Loading, PageHead, Section, StatusDot, Tags, useToast } from "../components/ui";
import { isAdmin, useMe } from "../lib/session";

export function TemplateDetail() {
  const { id = "" } = useParams();
  const template = useTemplate(id);
  const me = useMe();
  if (template.isPending) return <Loading rows={6} label="Loading the template" />;
  if (template.error) {
    if (template.error instanceof ApiError && template.error.status === 404)
      return (
        <>
          <PageHead title="Template not found" crumb={<Link to="/templates">Templates</Link>} />
          <Empty title={`There is no template named ${id}.`} />
        </>
      );
    return <ErrorState error={template.error} retry={() => void template.refetch()} />;
  }
  const t = template.data;
  return (
    <>
      <PageHead
        crumb={<Link to="/templates">Templates</Link>}
        title={t.name}
        description={t.description}
        actions={<span className="mono small muted">{`${t.id} ${t.version} · ${t.source}`}</span>}
      />
      <div className="columns">
        <div className="stack">
          {isAdmin(me) ? <CreateForm template={t} /> : null}
          <Section title="Procedure">
            <pre className="code-block prose-block">{t.instructions}</pre>
          </Section>
          {t.examples.length ? (
            <Section title="Example tasks">
              <ul className="plain-list">
                {t.examples.map((example) => (
                  <li key={example}>{example}</li>
                ))}
              </ul>
            </Section>
          ) : null}
        </div>
        <aside className="side-panel" aria-label="What the template is made of">
          <div>
            <h3>In this workspace</h3>
            <Checks checks={t.checks} />
          </div>
          <div>
            <h3>Models</h3>
            <ul className="checks">
              {t.fits.map((fit) => (
                <li key={fit.deployment}>
                  <StatusDot tone={fit.ok ? "ok" : "bad"}>
                    <span className="mono">{fit.deployment}</span>
                  </StatusDot>
                  {fit.checks
                    .filter((c) => !c.ok)
                    .map((c) => (
                      <div className="small muted" key={c.requirement}>
                        {c.detail}
                      </div>
                    ))}
                </li>
              ))}
            </ul>
          </div>
          <div>
            <h3>Tools</h3>
            <Tags values={t.tools} />
          </div>
          <Contract id={t.contract} />
          {t.policy.length ? (
            <div>
              <h3>Policy overlay</h3>
              <Rules rules={t.policy} />
            </div>
          ) : null}
        </aside>
      </div>
    </>
  );
}

function Checks({ checks }: { checks: RequirementCheck[] }) {
  if (!checks.length) return <p className="small muted">No requirements beyond a model.</p>;
  return (
    <ul className="checks">
      {checks.map((c) => (
        <li key={c.requirement}>
          <StatusDot tone={c.ok ? "ok" : c.hard ? "bad" : "warn"}>{c.requirement}</StatusDot>
          <div className="small muted">{c.detail}</div>
        </li>
      ))}
    </ul>
  );
}

export function Rules({ rules }: { rules: PolicyOverlayRule[] }) {
  return (
    <ul className="checks">
      {rules.map((rule, index) => (
        <li key={`${rule.name}-${index}`}>
          <span className={`policy ${rule.decision}`}>{rule.decision.replaceAll("_", " ")}</span>{" "}
          <span className="small">
            {rule.effects?.length ? `${rule.effects.join(" or ")} calls` : `tools ${rule.tools ?? "*"}`}
            {rule.outside && Object.keys(rule.outside).length
              ? ` outside ${[...new Set(Object.values(rule.outside).flat())].join(", ")}`
              : ""}
          </span>
          {rule.reason ? <div className="small muted">{rule.reason}</div> : null}
        </li>
      ))}
    </ul>
  );
}

function Contract({ id }: { id: string | null | undefined }) {
  const contracts = useContracts();
  if (!id) return null;
  const contract = contracts.data?.find((c) => c.id === id);
  return (
    <div>
      <h3>Output contract</h3>
      <p className="small">
        <span className="tag">{id}</span> {contract?.description}
      </p>
      {contract ? (
        <>
          <p className="small muted">Hollis accepts a submission only if:</p>
          <ul className="small rule-list">
            {contract.rules.map((rule) => (
              <li key={rule}>{rule}</li>
            ))}
          </ul>
        </>
      ) : null}
    </div>
  );
}

function CreateForm({ template }: { template: Detail }) {
  const navigate = useNavigate();
  const toast = useToast();
  const create = useCreateFromTemplate();
  const collections = useCollections();
  const agents = useAgents();
  const usable = useMemo(() => template.fits.filter((f) => f.ok), [template.fits]);
  const [name, setName] = useState(template.agents.length ? "" : template.id);
  const [model, setModel] = useState(usable[0]?.deployment ?? "");
  const [values, setValues] = useState<Record<string, string>>(() =>
    Object.fromEntries(template.parameters.map((p) => [p.name, p.default ?? ""])),
  );
  const [chosenCollections, setCollections] = useState<string[]>([]);
  const [delegates, setDelegates] = useState<string[]>([]);
  const requires = template.requires as { workspace?: { collections?: boolean; delegates?: number } };
  const needsKnowledge = Boolean(requires.workspace?.collections);
  const needsDelegates = (requires.workspace?.delegates ?? 0) > 0;
  const blocked = template.checks.filter((c) => c.hard && !c.ok);
  const error = create.error instanceof ApiError ? create.error : null;

  return (
    <Section title="Create an agent from it">
      {blocked.length ? (
        <div className="notice danger" style={{ marginBottom: 16 }}>
          <p>
            This workspace is missing what the template needs:{" "}
            {blocked.map((c) => `${c.requirement} (${c.detail})`).join("; ")}.
          </p>
        </div>
      ) : null}
      <form
        className="form"
        onSubmit={(event) => {
          event.preventDefault();
          create.mutate(
            {
              template: template.id,
              name,
              model,
              parameters: values,
              overrides: {
                ...(chosenCollections.length ? { knowledge: { collections: chosenCollections, labels: [] } } : {}),
                ...(delegates.length ? { delegates } : {}),
              },
            },
            {
              onSuccess: (agent) => {
                toast(`Created ${agent.name} from ${template.name}.`);
                void navigate(`/agents/${agent.name}`);
              },
            },
          );
        }}
      >
        <div className="form-row">
          <Field label="Agent name" hint="Lowercase letters, digits and dashes." error={error?.fieldError("name")}>
            {(props) => (
              <input
                {...props}
                type="text"
                value={name}
                onChange={(event) => setName(event.target.value)}
                pattern="[a-z0-9][a-z0-9-]{0,62}"
                required
              />
            )}
          </Field>
          <Field label="Model" hint="Deployments that meet the template’s model needs.">
            {(props) => (
              <select {...props} value={model} onChange={(event) => setModel(event.target.value)} required>
                {usable.length === 0 ? <option value="">No deployment fits</option> : null}
                {usable.map((fit) => (
                  <option key={fit.deployment} value={fit.deployment}>
                    {fit.deployment}
                    {fit.deployment === "demo" ? " (deterministic rules, not a language model)" : ""}
                  </option>
                ))}
              </select>
            )}
          </Field>
        </div>
        {template.parameters.map((parameter) => (
          <Field key={parameter.name} label={parameter.name} hint={parameter.description}>
            {(props) =>
              parameter.choices?.length ? (
                <select
                  {...props}
                  value={values[parameter.name] ?? ""}
                  onChange={(event) => setValues((v) => ({ ...v, [parameter.name]: event.target.value }))}
                >
                  {parameter.choices.map((choice) => (
                    <option key={choice}>{choice}</option>
                  ))}
                </select>
              ) : (
                <input
                  {...props}
                  type="text"
                  value={values[parameter.name] ?? ""}
                  required={parameter.default === null}
                  onChange={(event) => setValues((v) => ({ ...v, [parameter.name]: event.target.value }))}
                />
              )
            }
          </Field>
        ))}
        {needsKnowledge ? (
          <fieldset className="choices">
            <legend>Knowledge collections it searches</legend>
            {collections.data?.map((c) => (
              <label className="check small" key={c.name}>
                <input
                  type="checkbox"
                  checked={chosenCollections.includes(c.name)}
                  onChange={(event) =>
                    setCollections((current) =>
                      event.target.checked ? [...current, c.name] : current.filter((n) => n !== c.name),
                    )
                  }
                />
                <span>{c.name}</span>
              </label>
            ))}
          </fieldset>
        ) : null}
        {needsDelegates ? (
          <fieldset className="choices">
            <legend>Agents it delegates to</legend>
            <span className="hint small muted">
              Delegated runs get at most what this agent’s run may reach: give it the knowledge they need too.
            </span>
            {agents.data?.map((a) => (
              <label className="check small" key={a.name}>
                <input
                  type="checkbox"
                  checked={delegates.includes(a.name)}
                  onChange={(event) =>
                    setDelegates((current) =>
                      event.target.checked ? [...current, a.name] : current.filter((n) => n !== a.name),
                    )
                  }
                />
                <span>
                  {a.name}
                  {a.description ? <span className="muted"> — {a.description}</span> : null}
                </span>
              </label>
            ))}
          </fieldset>
        ) : null}
        <FormError error={create.error} />
        <div className="form-actions">
          <button className="btn primary" type="submit" disabled={create.isPending || blocked.length > 0 || !model}>
            Create agent
          </button>
        </div>
      </form>
    </Section>
  );
}
