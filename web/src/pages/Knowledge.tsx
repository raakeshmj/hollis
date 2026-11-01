import { useState } from "react";
import { Link } from "react-router";
import { useCollections, useCreateCollection, useSearch } from "../api/hooks";
import { Icon } from "../components/Icon";
import { Async, Empty, Field, FormError, PageHead, Section, Tags, Time, useToast } from "../components/ui";
import { isAdmin, useMe } from "../lib/session";

export function Knowledge() {
  const collections = useCollections();
  const admin = isAdmin(useMe());
  return (
    <>
      <PageHead
        title="Knowledge"
        description="Documents agents search and cite. Access is checked before anything is scored: you, and runs started by you, see only collections and labels you are granted. Document text never changes access or instructions."
      />
      <Section title="Collections">
        <Async
          query={collections}
          empty={(items) =>
            items.length === 0 ? (
              <Empty title="No collections you can read">
                {admin ? "Create one below, then add documents to it." : "An admin grants collections."}
              </Empty>
            ) : null
          }
        >
          {(items) => (
            <div className="table-wrap" role="region" aria-label="Collections" tabIndex={0}>
              <table>
                <thead>
                  <tr>
                    <th scope="col">Collection</th>
                    <th scope="col" className="num">
                      Documents
                    </th>
                    <th scope="col" className="num">
                      Chunks
                    </th>
                    <th scope="col" className="hide-narrow">
                      Embeddings
                    </th>
                    <th scope="col">Created</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((c) => (
                    <tr key={c.name}>
                      <td>
                        <Link className="row-link" to={`/knowledge/${c.name}`}>
                          {c.name}
                        </Link>
                        {c.description ? <div className="small muted">{c.description}</div> : null}
                      </td>
                      <td className="num">{c.sources}</td>
                      <td className="num">{c.chunks}</td>
                      <td className="small mono hide-narrow">{c.embedding ?? "lexical only"}</td>
                      <td className="muted nowrap">
                        <Time value={c.created_at} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Async>
      </Section>
      <SearchPanel />
      {admin ? <CreateCollection /> : null}
    </>
  );
}

function SearchPanel() {
  const search = useSearch();
  const [query, setQuery] = useState("");
  return (
    <Section title="Search" aside={<p>The same retrieval agents use, with your access.</p>}>
      <form
        className="row"
        role="search"
        onSubmit={(event) => {
          event.preventDefault();
          if (query.trim()) search.mutate({ query: query.trim(), collections: [] });
        }}
      >
        <label htmlFor="knowledge-search" className="visually-hidden">
          Search documents
        </label>
        <input
          id="knowledge-search"
          type="search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="e.g. how do we rotate keys?"
          style={{ flex: 1, maxWidth: 520 }}
        />
        <button type="submit" className="btn" disabled={search.isPending || !query.trim()}>
          <Icon name="search" size={14} /> Search
        </button>
      </form>
      <FormError error={search.error} />
      {search.data ? (
        <div style={{ marginTop: 16 }}>
          {search.data.notes.map((note) => (
            <p key={note} className="small muted">
              {note}
            </p>
          ))}
          {search.data.hits.length === 0 ? <p className="muted">No passages matched.</p> : null}
          {search.data.hits.map(({ passage, source_key }) => (
            <div className="passage" key={passage.chunk_id}>
              <span className="cite">{passage.key}</span> <strong>{source_key}</strong>{" "}
              <span className="muted">
                {passage.collection} · {passage.location}
              </span>
              <p style={{ whiteSpace: "pre-wrap" }}>{passage.text}</p>
              <div className="row small muted" style={{ marginTop: 4 }}>
                <span>found by {passage.retrievers.join(" + ")}</span>
                {passage.labels.length ? <Tags values={passage.labels} /> : null}
              </div>
            </div>
          ))}
          <p className="small muted" style={{ marginTop: 8 }}>
            {search.data.elapsed_ms.toFixed(0)} ms
          </p>
        </div>
      ) : null}
    </Section>
  );
}

function CreateCollection() {
  const create = useCreateCollection();
  const toast = useToast();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  return (
    <Section title="New collection">
      <form
        className="form"
        onSubmit={(event) => {
          event.preventDefault();
          create.mutate(
            { name: name.trim(), description: description.trim() },
            {
              onSuccess: (c) => {
                toast(`Created ${c.name}.`);
                setName("");
                setDescription("");
              },
            },
          );
        }}
      >
        <div className="form-row">
          <Field label="Name" hint="Lowercase letters, digits, dot, dash, underscore.">
            {(props) => (
              <input {...props} type="text" value={name} onChange={(e) => setName(e.target.value)} pattern="[a-z0-9][a-z0-9._-]{0,63}" required />
            )}
          </Field>
          <Field label="Description">
            {(props) => <input {...props} type="text" value={description} onChange={(e) => setDescription(e.target.value)} />}
          </Field>
        </div>
        <FormError error={create.error} />
        <div className="form-actions">
          <button type="submit" className="btn primary" disabled={create.isPending}>
            Create collection
          </button>
        </div>
      </form>
    </Section>
  );
}
