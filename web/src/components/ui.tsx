/** Shared interface parts. Status is never conveyed by color alone: every phase has a glyph and
 * a word. */

import { createContext, useCallback, useContext, useEffect, useId, useRef, useState, type ReactNode } from "react";
import { ApiError } from "../api/client";
import type { RunPhase } from "../api/types";
import { ago, dateTime, label } from "../lib/format";
import { Icon } from "./Icon";

export function PageHead({
  title,
  description,
  crumb,
  actions,
}: {
  title: ReactNode;
  description?: ReactNode;
  crumb?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <header className="page-head">
      <div>
        {crumb ? <span className="crumb">{crumb}</span> : null}
        <h1>{title}</h1>
        {description ? <p>{description}</p> : null}
      </div>
      {actions ? <div className="page-actions">{actions}</div> : null}
    </header>
  );
}

export function Section({
  title,
  aside,
  children,
  id,
}: {
  title: ReactNode;
  aside?: ReactNode;
  children: ReactNode;
  id?: string;
}) {
  const generated = useId();
  const heading = id ?? generated;
  return (
    <section className="section" aria-labelledby={heading}>
      <div className="section-head">
        <h2 id={heading}>{title}</h2>
        {aside}
      </div>
      {children}
    </section>
  );
}

const PHASE_GLYPHS: Record<RunPhase, ReactNode> = {
  completed: <circle cx="6" cy="6" r="4.5" fill="currentColor" />,
  running: <path d="M6 1.5a4.5 4.5 0 0 1 0 9Z" fill="currentColor" stroke="currentColor" strokeWidth="1" />,
  queued: <circle cx="6" cy="6" r="2" fill="currentColor" />,
  waiting_approval: <path d="M6 1 11 6 6 11 1 6Z" fill="currentColor" />,
  failed: <path d="m2.5 2.5 7 7m0-7-7 7" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />,
  cancelled: <circle cx="6" cy="6" r="4" fill="none" stroke="currentColor" strokeWidth="1.5" />,
  interrupted: (
    <circle cx="6" cy="6" r="4" fill="none" stroke="currentColor" strokeWidth="1.5" strokeDasharray="2 2" />
  ),
};

export function Phase({ phase }: { phase: RunPhase }) {
  return (
    <span className={`phase ${phase}`}>
      <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">
        {PHASE_GLYPHS[phase]}
      </svg>
      {label(phase)}
    </span>
  );
}

export function Time({ value, relative = true }: { value: string | null | undefined; relative?: boolean }) {
  if (!value) return null;
  return (
    <time dateTime={value} title={dateTime(value)}>
      {relative ? ago(value) : dateTime(value)}
    </time>
  );
}

export function Tags({ values, empty = "none" }: { values: readonly string[]; empty?: string }) {
  if (!values.length) return <span className="muted">{empty}</span>;
  return (
    <span className="tags">
      {values.map((value) => (
        <span className="tag" key={value}>
          {value}
        </span>
      ))}
    </span>
  );
}

export function Empty({ title, children, actions }: { title: string; children?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="empty">
      <h3>{title}</h3>
      {children ? <p>{children}</p> : null}
      {actions ? <div className="row">{actions}</div> : null}
    </div>
  );
}

export function Loading({ rows = 4, label: text = "Loading" }: { rows?: number; label?: string }) {
  return (
    <div role="status" aria-live="polite">
      <span className="visually-hidden">{text}…</span>
      {Array.from({ length: rows }, (_, i) => (
        <span key={i} className="skeleton" style={{ width: `${92 - i * 11}%` }} aria-hidden="true" />
      ))}
    </div>
  );
}

export function ErrorState({ error, retry }: { error: unknown; retry?: () => void }) {
  const message = error instanceof ApiError ? error.detail : "Something went wrong while loading this.";
  const code = error instanceof ApiError ? error.code : null;
  return (
    <div className="notice danger" role="alert">
      <div>
        <p>
          <strong>{code === "forbidden" ? "Not available to you" : "Could not load"}</strong>
        </p>
        <p>{message}</p>
        {retry ? (
          <div className="row" style={{ marginTop: 8 }}>
            <button type="button" className="btn small" onClick={retry}>
              <Icon name="refresh" size={14} /> Try again
            </button>
          </div>
        ) : null}
      </div>
    </div>
  );
}

/** Loading, error and empty handling around data. */
export function Async<T>({
  query,
  children,
  empty,
  rows,
}: {
  query: { data: T | undefined; error: unknown; isPending: boolean; refetch: () => unknown };
  children: (data: T) => ReactNode;
  empty?: (data: T) => ReactNode | null;
  rows?: number;
}) {
  if (query.isPending) return <Loading rows={rows} />;
  if (query.error) return <ErrorState error={query.error} retry={() => void query.refetch()} />;
  if (query.data === undefined) return null;
  const blank = empty?.(query.data);
  return <>{blank ?? children(query.data)}</>;
}

export function Arguments({ value }: { value: unknown }) {
  if (value && typeof value === "object" && !Array.isArray(value)) {
    const entries = Object.entries(value as Record<string, unknown>).filter(([, v]) => v !== null && v !== undefined);
    if (!entries.length) return <span className="muted small">no arguments</span>;
    return (
      <dl className="args-table">
        {entries.map(([key, v]) => (
          <div key={key} style={{ display: "contents" }}>
            <dt>{key}</dt>
            <dd>{typeof v === "string" ? v : JSON.stringify(v, null, 2)}</dd>
          </div>
        ))}
      </dl>
    );
  }
  return <pre className="code-block">{JSON.stringify(value, null, 2)}</pre>;
}

export function Field({
  label: text,
  hint,
  error,
  children,
}: {
  label: string;
  hint?: ReactNode;
  error?: string | undefined;
  children: (props: { id: string; "aria-describedby"?: string; "aria-invalid"?: boolean }) => ReactNode;
}) {
  const id = useId();
  const hintId = `${id}-hint`;
  const errorId = `${id}-error`;
  const describedBy = [hint ? hintId : null, error ? errorId : null].filter(Boolean).join(" ") || undefined;
  return (
    <div className="field">
      <label htmlFor={id}>{text}</label>
      {children({ id, "aria-describedby": describedBy, "aria-invalid": error ? true : undefined })}
      {hint ? (
        <span className="hint" id={hintId}>
          {hint}
        </span>
      ) : null}
      {error ? (
        <span className="error" id={errorId}>
          {error}
        </span>
      ) : null}
    </div>
  );
}

export function FormError({ error }: { error: unknown }) {
  if (!error) return null;
  const detail = error instanceof ApiError ? error.detail : String(error);
  return (
    <div className="notice danger" role="alert">
      <p>{detail}</p>
    </div>
  );
}

// --- confirmation dialog ------------------------------------------------------------------------

export function Confirm({
  open,
  title,
  children,
  confirmLabel,
  danger = false,
  busy = false,
  onConfirm,
  onCancel,
}: {
  open: boolean;
  title: string;
  children: ReactNode;
  confirmLabel: string;
  danger?: boolean;
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const headingId = useId();
  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (open && !dialog.open) dialog.showModal?.();
    if (!open && dialog.open) dialog.close?.();
  }, [open]);
  return (
    <dialog ref={ref} aria-labelledby={headingId} onCancel={onCancel} onClose={onCancel}>
      <h2 id={headingId}>{title}</h2>
      <div>{children}</div>
      <div className="form-actions">
        <button type="button" className="btn" onClick={onCancel}>
          Cancel
        </button>
        <button
          type="button"
          className={danger ? "btn danger solid" : "btn primary"}
          onClick={onConfirm}
          disabled={busy}
        >
          {confirmLabel}
        </button>
      </div>
    </dialog>
  );
}

// --- toasts ------------------------------------------------------------------------------------

interface Toast {
  id: number;
  text: string;
  kind: "info" | "error";
}

const ToastContext = createContext<(text: string, kind?: Toast["kind"]) => void>(() => undefined);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const push = useCallback((text: string, kind: Toast["kind"] = "info") => {
    const id = Date.now() + Math.random();
    setToasts((current) => [...current.slice(-3), { id, text, kind }]);
    window.setTimeout(() => setToasts((current) => current.filter((t) => t.id !== id)), 5000);
  }, []);
  return (
    <ToastContext.Provider value={push}>
      {children}
      <div className="toasts" role="status" aria-live="polite">
        {toasts.map((toast) => (
          <div key={toast.id} className={`toast ${toast.kind}`}>
            {toast.text}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export const useToast = () => useContext(ToastContext);

export function errorText(error: unknown): string {
  return error instanceof ApiError ? error.detail : "Something went wrong.";
}

export function CopyButton({ value, label: text = "Copy" }: { value: string; label?: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      className="btn small"
      onClick={() => {
        void navigator.clipboard?.writeText(value).then(() => {
          setCopied(true);
          window.setTimeout(() => setCopied(false), 1500);
        });
      }}
    >
      <Icon name={copied ? "check" : "copy"} size={14} />
      {copied ? "Copied" : text}
    </button>
  );
}

export function StatusDot({ tone, children }: { tone: "ok" | "warn" | "bad" | "off"; children: ReactNode }) {
  return <span className={`status-dot ${tone}`}>{children}</span>;
}

export function Policy({ decision }: { decision: string }) {
  return <span className={`policy ${decision}`}>{label(decision)}</span>;
}

export function Pager({
  offset,
  limit,
  total,
  onChange,
}: {
  offset: number;
  limit: number;
  total: number;
  onChange: (offset: number) => void;
}) {
  if (total <= limit) return null;
  const end = Math.min(offset + limit, total);
  return (
    <nav className="pager" aria-label="Pages">
      <span>
        {offset + 1}–{end} of {total}
      </span>
      <span className="row">
        <button type="button" className="btn small" disabled={offset === 0} onClick={() => onChange(Math.max(0, offset - limit))}>
          Newer
        </button>
        <button type="button" className="btn small" disabled={end >= total} onClick={() => onChange(offset + limit)}>
          Older
        </button>
      </span>
    </nav>
  );
}
