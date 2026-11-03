import { Link } from "react-router";
import { useTemplates } from "../api/hooks";
import type { TemplateView } from "../api/types";
import { Async, Empty, PageHead, StatusDot } from "../components/ui";

/** The template gallery: first-party specialists and the workspace's own templates. */
export function Templates() {
  const templates = useTemplates();
  return (
    <>
      <PageHead
        title="Templates"
        description="Blueprints for specialized agents: focused tools, a procedure, policy that can only tighten, and an output contract Hollis checks against each run’s log. An agent made from a template is an ordinary agent you can change."
      />
      <Async
        query={templates}
        empty={(items) => (items.length === 0 ? <Empty title="No templates are available." /> : null)}
      >
        {(items) => (
          <ul className="gallery" aria-label="Templates">
            {items.map((template) => (
              <TemplateCard key={template.id} template={template} />
            ))}
          </ul>
        )}
      </Async>
    </>
  );
}

function TemplateCard({ template }: { template: TemplateView }) {
  const blocking = template.checks.filter((c) => c.hard && !c.ok);
  const warnings = template.checks.filter((c) => !c.hard && !c.ok);
  return (
    <li className="gallery-card">
      <div className="gallery-card-head">
        <h2>
          <Link className="row-link" to={`/templates/${template.id}`}>
            {template.name}
          </Link>
        </h2>
        <span className="mono small muted">
          {template.id} {template.version}
        </span>
      </div>
      <p className="small">{template.description}</p>
      <dl className="facts small">
        <dt>Output</dt>
        <dd>{template.contract ? <span className="tag">{template.contract}</span> : <span className="muted">free text</span>}</dd>
        <dt>Built from</dt>
        <dd>{template.capabilities.length ? template.capabilities.join(", ") : <span className="muted">its own tools</span>}</dd>
        {template.agents.length ? (
          <>
            <dt>Agents</dt>
            <dd>{template.agents.join(", ")}</dd>
          </>
        ) : null}
      </dl>
      <div className="gallery-card-foot">
        {blocking.length ? (
          <StatusDot tone="bad">Needs {blocking.map((c) => c.requirement).join(", ")}</StatusDot>
        ) : warnings.length ? (
          <StatusDot tone="warn">Ready; no {warnings.map((c) => c.requirement).join(", ")}</StatusDot>
        ) : (
          <StatusDot tone="ok">Ready in this workspace</StatusDot>
        )}
        {template.source !== "builtin" ? <span className="tag">{template.source}</span> : null}
      </div>
    </li>
  );
}
