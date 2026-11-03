import { useState, type ReactNode } from "react";
import { Navigate, NavLink, Outlet, useLocation, useNavigate } from "react-router";
import { ApiError } from "../api/client";
import { useApprovals, useHealth, useSession, useSignOut } from "../api/hooks";
import { useTheme } from "../lib/theme";
import { Icon, type IconName } from "./Icon";
import { Lockup } from "./Mark";
import { Loading } from "./ui";

const WORK: [string, string, IconName][] = [
  ["/", "Overview", "overview"],
  ["/runs", "Runs", "runs"],
  ["/approvals", "Approvals", "approvals"],
];
const BUILD: [string, string, IconName][] = [
  ["/agents", "Agents", "agents"],
  ["/templates", "Templates", "templates"],
  ["/tools", "Tools", "tools"],
  ["/models", "Models", "models"],
  ["/knowledge", "Knowledge", "knowledge"],
  ["/memory", "Memory", "memory"],
];

function NavItem({ to, label, icon, badge }: { to: string; label: string; icon: IconName; badge?: ReactNode }) {
  return (
    <NavLink to={to} end={to === "/"}>
      <Icon name={icon} />
      {label}
      {badge}
    </NavLink>
  );
}

export function Shell() {
  const session = useSession();
  const location = useLocation();
  const navigate = useNavigate();
  const health = useHealth();
  const approvals = useApprovals();
  const signOut = useSignOut();
  const [theme, cycleTheme] = useTheme();
  // The small-screen navigation closes itself on navigation: it is open only on the page where
  // it was opened.
  const [navOpenOn, setNavOpenOn] = useState<string | null>(null);
  const navOpen = navOpenOn === location.pathname;
  const setNavOpen = (open: boolean) => setNavOpenOn(open ? location.pathname : null);

  if (session.isPending) {
    return (
      <div className="signin">
        <Loading rows={3} label="Signing in" />
      </div>
    );
  }
  if (session.error instanceof ApiError && session.error.status === 401) {
    const next = location.pathname + location.search;
    return <Navigate to={`/login${next !== "/" ? `?next=${encodeURIComponent(next)}` : ""}`} replace />;
  }
  const me = session.data?.principal;
  const pending = approvals.data?.length ?? 0;
  const themeIcon: IconName = theme === "light" ? "sun" : theme === "dark" ? "moon" : "system";

  return (
    <div className={`shell${navOpen ? " nav-open" : ""}`}>
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <div className="topbar">
        <button
          type="button"
          className="btn quiet"
          aria-label="Open navigation"
          aria-expanded={navOpen}
          aria-controls="sidebar"
          onClick={() => setNavOpen(true)}
        >
          <Icon name="menu" />
        </button>
        <Lockup size={20} />
      </div>
      {navOpen ? <div className="scrim" aria-hidden="true" onClick={() => setNavOpen(false)} /> : null}
      <div className="sidebar-col">
      <aside className="sidebar" id="sidebar" aria-label="Main">
        <div>
          <NavLink to="/" className="brand" aria-label="Hollis overview">
            <Lockup size={22} />
          </NavLink>
          <div className="workspace-name" title={health.data?.workspace_path}>
            {health.data ? `${health.data.workspace} workspace` : " "}
          </div>
        </div>
        <nav className="nav" aria-label="Sections">
          {WORK.map(([to, text, icon]) => (
            <NavItem
              key={to}
              to={to}
              label={text}
              icon={icon}
              badge={
                to === "/approvals" && pending > 0 ? (
                  <span className="count" aria-label={`${pending} waiting`}>
                    {pending}
                  </span>
                ) : undefined
              }
            />
          ))}
          <span className="nav-group-label">Build</span>
          {BUILD.map(([to, text, icon]) => (
            <NavItem key={to} to={to} label={text} icon={icon} />
          ))}
          <span className="nav-group-label">Workspace</span>
          <NavItem to="/settings" label="Settings" icon="settings" />
        </nav>
        <div className="sidebar-foot">
          {me ? (
            <div className="who">
              <strong>{me.name}</strong>
              <span>{me.role}</span>
            </div>
          ) : null}
          <div className="foot-actions">
            <button
              type="button"
              className="btn small quiet"
              onClick={cycleTheme}
              aria-label={`Theme: ${theme}. Change theme`}
              title={`Theme: ${theme}`}
            >
              <Icon name={themeIcon} size={15} />
              {theme === "system" ? "System" : theme === "light" ? "Light" : "Dark"}
            </button>
            <button
              type="button"
              className="btn small quiet"
              onClick={() => signOut.mutate(undefined, { onSettled: () => void navigate("/login") })}
            >
              <Icon name="out" size={15} /> Sign out
            </button>
          </div>
        </div>
      </aside>
      </div>
      <main className="main" id="main" tabIndex={-1}>
        <div className="page">
          <Outlet />
        </div>
      </main>
    </div>
  );
}
