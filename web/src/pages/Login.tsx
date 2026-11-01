import { useEffect, useRef, useState } from "react";
import { Navigate, useNavigate, useSearchParams } from "react-router";
import { ApiError } from "../api/client";
import { useSession, useSignIn } from "../api/hooks";
import { Lockup } from "../components/Mark";
import { Field, Loading } from "../components/ui";

/** Only paths on this site are accepted as a destination after signing in: not `//host` or
 * `/\host`, which browsers read as another site. */
export function safeNext(value: string | null): string {
  return value && /^\/(?![/\\])/.test(value) ? value : "/";
}

export function Login() {
  const [params] = useSearchParams();
  const next = safeNext(params.get("next"));
  const navigate = useNavigate();
  const session = useSession();
  const signIn = useSignIn();
  const [token, setToken] = useState("");
  // A sign-in link carries a single-use token in the URL fragment, which is never sent to the
  // server or kept in history: read it once, remove it from the address bar, exchange it.
  const [linked] = useState(() => new URLSearchParams(window.location.hash.slice(1)).get("token"));
  const [viaLink, setViaLink] = useState(linked !== null);
  const exchanged = useRef(false);

  useEffect(() => {
    if (!linked || exchanged.current) return;
    exchanged.current = true;
    window.history.replaceState(null, "", window.location.pathname + window.location.search);
    signIn.mutate(linked, { onSuccess: () => void navigate(next, { replace: true }) });
  }, [linked, navigate, next, signIn]);

  if (session.data && !signIn.isPending && !viaLink) return <Navigate to={next} replace />;

  const error =
    signIn.error instanceof ApiError
      ? signIn.error.status === 401
        ? viaLink
          ? "This sign-in link has expired or was already used. Ask for a new one (`hollis login-link`), or paste a token."
          : "That token is not valid. Check it, or create a new one with `hollis tokens create`."
        : signIn.error.detail
      : undefined;

  return (
    <main className="signin">
      <div className="signin-panel">
        <div className="brand">
          <Lockup size={28} />
        </div>
        <h1>Sign in</h1>
        <p>
          Open the link <code>hollis serve</code> printed, or paste an API token. Tokens are created with{" "}
          <code>hollis tokens create</code>.
        </p>
        {signIn.isPending && viaLink ? (
          <Loading rows={2} label="Signing in" />
        ) : (
          <form
            className="form"
            onSubmit={(event) => {
              event.preventDefault();
              setViaLink(false);
              signIn.mutate(token.trim(), { onSuccess: () => void navigate(next, { replace: true }) });
            }}
          >
            <Field label="API token" error={error}>
              {(props) => (
                <input
                  {...props}
                  type="password"
                  autoComplete="off"
                  spellCheck={false}
                  className="code"
                  value={token}
                  onChange={(event) => setToken(event.target.value)}
                  placeholder="hx_…"
                  required
                />
              )}
            </Field>
            <div className="form-actions">
              <button className="btn primary" type="submit" disabled={signIn.isPending || token.trim().length < 8}>
                Sign in
              </button>
            </div>
          </form>
        )}
      </div>
    </main>
  );
}
