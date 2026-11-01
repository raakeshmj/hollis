import { useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router";
import { useDeleteCollection, useDocumentAction, useSource, useSources, useUpload } from "../api/hooks";
import type { SourceView } from "../api/types";
import { Icon } from "../components/Icon";
import { Async, Confirm, Empty, ErrorState, Field, Loading, PageHead, Section, Tags, Time, errorText, useToast } from "../components/ui";
import { bytes, label } from "../lib/format";
import { isAdmin, useMe } from "../lib/session";

export function Collection() {
  const { name = "" } = useParams();
  const sources = useSources(name);
  const admin = isAdmin(useMe());
  const [selected, setSelected] = useState<string | null>(null);
  const [dropping, setDropping] = useState(false);
  const drop = useDeleteCollection();
  const navigate = useNavigate();
  const toast = useToast();

  return (
    <>
      <PageHead
        crumb={<Link to="/knowledge">Knowledge</Link>}
        title={name}
        description="Documents are split into sections and chunks, indexed for keyword and similarity search, and cited by key."
        actions={
          admin ? (
            <button type="button" className="btn danger" onClick={() => setDropping(true)}>
              Delete collection
            </button>
          ) : null
        }
      />
      {admin ? <Upload collection={name} /> : null}
      <Section title="Documents">
        <Async
          query={sources}
          empty={(items) =>
            items.length === 0 ? (
              <Empty title="No documents you can see">{admin ? "Upload some above." : "Documents with labels you lack are hidden."}</Empty>
            ) : null
          }
        >
          {(items) => (
            <div className="columns">
              <div className="table-wrap" role="region" aria-label="Documents" tabIndex={0}>
                <table>
                  <thead>
                    <tr>
                      <th scope="col">Document</th>
                      <th scope="col" className="num">
                        Chunks
                      </th>
                      <th scope="col">Labels</th>
                      <th scope="col" className="hide-narrow">
                        Ingested
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {items.map((s) => (
                      <tr key={s.source_id} aria-selected={selected === s.source_id}>
                        <td>
                          <button
                            type="button"
                            className="btn quiet small"
                            style={{ padding: 0, height: "auto", fontWeight: 500, color: "var(--ink)" }}
                            onClick={() => setSelected(s.source_id)}
                          >
                            {s.key}
                          </button>
                          <div className="small muted">
                            {s.title} · v{s.version} · {bytes(s.size_bytes)}
                            {s.flagged_chunks ? ` · ${s.flagged_chunks} flagged` : ""}
                          </div>
                        </td>
                        <td className="num">{s.chunks}</td>
                        <td>
                          <Tags values={s.labels} empty="everyone with the collection" />
                        </td>
                        <td className="muted nowrap hide-narrow">
                          <Time value={s.ingested_at} />
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div>
                {selected ? (
                  <SourcePanel
                    collection={name}
                    id={selected}
                    source={items.find((s) => s.source_id === selected)}
                    admin={admin}
                    onClose={() => setSelected(null)}
                  />
                ) : (
                  <p className="small muted">Select a document to see its sections and chunks.</p>
                )}
              </div>
            </div>
          )}
        </Async>
      </Section>
      <Confirm
        open={dropping}
        title={`Delete ${name}?`}
        confirmLabel="Delete collection"
        danger
        busy={drop.isPending}
        onCancel={() => setDropping(false)}
        onConfirm={() =>
          drop.mutate(name, {
            onSuccess: () => {
              toast(`Deleted ${name}.`);
              void navigate("/knowledge");
            },
            onError: (error) => toast(errorText(error), "error"),
            onSettled: () => setDropping(false),
          })
        }
      >
        <p>
          Its documents are removed from the index. Runs that already retrieved passages keep them in their event logs.
        </p>
      </Confirm>
    </>
  );
}

function Upload({ collection }: { collection: string }) {
  const upload = useUpload(collection);
  const toast = useToast();
  const input = useRef<HTMLInputElement>(null);
  const [labels, setLabels] = useState("");
  const [busy, setBusy] = useState(false);

  const send = async (files: FileList | null) => {
    if (!files?.length) return;
    setBusy(true);
    const list = labels.split(/[\s,]+/).filter(Boolean);
    for (const file of Array.from(files)) {
      try {
        const result = await upload.mutateAsync({ file, key: file.name, labels: list });
        toast(`${file.name}: ${label(result.action)}${result.reason ? ` (${result.reason})` : ""}.`, result.action === "rejected" ? "error" : "info");
      } catch (error) {
        toast(`${file.name}: ${errorText(error)}`, "error");
      }
    }
    setBusy(false);
    if (input.current) input.current.value = "";
  };

  return (
    <Section title="Add documents" aside={<p>Markdown, text, HTML and PDF. Re-uploading a key replaces it.</p>}>
      <div className="form-row" style={{ maxWidth: 720 }}>
        <Field label="Files">
          {(props) => (
            <input
              {...props}
              ref={input}
              type="file"
              multiple
              accept=".md,.markdown,.txt,.rst,.html,.htm,.pdf,.json,.csv,.py,.ts,.js"
              disabled={busy}
              onChange={(event) => void send(event.target.files)}
            />
          )}
        </Field>
        <Field label="Access labels" hint="Optional, e.g. team:ops. Without labels, everyone with the collection sees it.">
          {(props) => <input {...props} type="text" className="code" value={labels} onChange={(e) => setLabels(e.target.value)} />}
        </Field>
      </div>
      {busy ? (
        <p className="small muted" role="status">
          <Icon name="upload" size={14} /> Uploading…
        </p>
      ) : null}
    </Section>
  );
}

function SourcePanel({
  collection,
  id,
  source,
  admin,
  onClose,
}: {
  collection: string;
  id: string;
  source: SourceView | undefined;
  admin: boolean;
  onClose: () => void;
}) {
  const detail = useSource(collection, id);
  const action = useDocumentAction(collection);
  const toast = useToast();
  const [labels, setLabels] = useState((source?.labels ?? []).join(", "));
  const [removing, setRemoving] = useState(false);
  if (detail.isPending) return <Loading rows={5} />;
  if (detail.error) return <ErrorState error={detail.error} />;
  const d = detail.data;
  return (
    <aside className="side-panel" aria-label={`Document ${d.source.key}`}>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h3 className="mono">{d.source.key}</h3>
        <button type="button" className="btn small quiet" onClick={onClose} aria-label="Close document">
          <Icon name="close" size={14} />
        </button>
      </div>
      <dl className="facts small">
        <dt>Title</dt>
        <dd>{d.source.title}</dd>
        <dt>Type</dt>
        <dd>{d.source.media_type}</dd>
        <dt>Trust</dt>
        <dd>{d.source.trust}</dd>
        <dt>Version</dt>
        <dd>{d.source.version}</dd>
      </dl>
      {d.sections.length ? (
        <div>
          <h3>Sections</h3>
          <ul className="small" style={{ margin: 0, paddingLeft: 16 }}>
            {d.sections.map((s) => (
              <li key={s.index}>
                {s.path.join(" › ") || s.title || "(untitled)"} <span className="muted">({s.chunks})</span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      <details>
        <summary>{d.chunks.length} chunks</summary>
        {d.chunks.map((c) => (
          <div className="passage" key={c.chunk_id}>
            <span className="muted small">
              {c.location} · {c.tokens} tokens
            </span>
            {c.flags.length ? <Tags values={c.flags} /> : null}
            <p style={{ whiteSpace: "pre-wrap" }}>{c.text}</p>
          </div>
        ))}
      </details>
      {admin ? (
        <form
          className="stack"
          onSubmit={(event) => {
            event.preventDefault();
            action.mutate(
              { key: d.source.key, labels: labels.split(/[\s,]+/).filter(Boolean) },
              { onSuccess: () => toast("Labels updated."), onError: (error) => toast(errorText(error), "error") },
            );
          }}
        >
          <Field label="Access labels">
            {(props) => <input {...props} type="text" className="code" value={labels} onChange={(e) => setLabels(e.target.value)} />}
          </Field>
          <div className="row">
            <button type="submit" className="btn small">
              Save labels
            </button>
            <button type="button" className="btn small danger" onClick={() => setRemoving(true)}>
              Remove document
            </button>
          </div>
        </form>
      ) : null}
      <Confirm
        open={removing}
        title={`Remove ${d.source.key}?`}
        confirmLabel="Remove"
        danger
        busy={action.isPending}
        onCancel={() => setRemoving(false)}
        onConfirm={() =>
          action.mutate(
            { key: d.source.key },
            {
              onSuccess: () => {
                toast(`Removed ${d.source.key}.`);
                onClose();
              },
              onSettled: () => setRemoving(false),
            },
          )
        }
      >
        <p>It is removed from the index. Event logs of runs that cited it keep their copies.</p>
      </Confirm>
    </aside>
  );
}
