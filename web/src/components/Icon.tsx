/** Interface icons drawn for Hollis: a 20px grid, 1.5px strokes, square-ish terminals echoing the
 * mark. The runs icon is the mark's own construction: a ring, a right-angle path, a square. */

const PATHS = {
  overview: (
    <>
      <rect x="3" y="3" width="14" height="14" rx="1.5" />
      <path d="M3 8.5h14M8.5 8.5V17" />
    </>
  ),
  runs: (
    <>
      <circle cx="6" cy="6" r="2.4" />
      <path d="M6 8.4v6.1h5.5" />
      <rect x="12.5" y="12" width="5" height="5" rx="0.6" fill="currentColor" stroke="none" />
    </>
  ),
  approvals: (
    <>
      <path d="M10 2.8 16 5v4.6c0 3.6-2.5 6.3-6 7.6-3.5-1.3-6-4-6-7.6V5Z" />
      <path d="m7.3 10 1.9 1.9 3.6-3.8" />
    </>
  ),
  agents: (
    <>
      <rect x="3" y="4" width="14" height="12" rx="1.5" />
      <circle cx="7.6" cy="9" r="1.8" />
      <path d="M5.4 13.2c.5-1 1.3-1.5 2.2-1.5s1.7.5 2.2 1.5M11.8 8.2h3M11.8 11.2h3" />
    </>
  ),
  tools: (
    <path d="M12.6 3.4a3.6 3.6 0 0 0-4 4.9l-5 5a1.5 1.5 0 0 0 2.1 2.1l5-5a3.6 3.6 0 0 0 4.9-4l-2.1 2.1-2.2-.6-.6-2.2Z" />
  ),
  templates: (
    <>
      <path d="M3.5 6.5v-3h3M13.5 3.5h3v3M16.5 13.5v3h-3M6.5 16.5h-3v-3" />
      <circle cx="7.8" cy="7.8" r="1.6" />
      <path d="M7.8 9.4v3.4h2.8" />
      <rect x="11.2" y="11.2" width="3" height="3" rx="0.4" fill="currentColor" stroke="none" />
    </>
  ),
  models: (
    <>
      <path d="m10 3 7 3.6-7 3.6-7-3.6Z" />
      <path d="m3 10.2 7 3.6 7-3.6M3 13.6l7 3.6 7-3.6" />
    </>
  ),
  knowledge: (
    <>
      <path d="M10 5.2C8.4 4 6.4 3.6 3.5 3.8v11.4c2.9-.2 4.9.2 6.5 1.4 1.6-1.2 3.6-1.6 6.5-1.4V3.8c-2.9-.2-4.9.2-6.5 1.4Z" />
      <path d="M10 5.2v11.4" />
    </>
  ),
  memory: (
    <>
      <path d="M5.5 3h9v14l-4.5-3.2L5.5 17Z" />
      <path d="M8.2 7.4h3.6" />
    </>
  ),
  settings: (
    <>
      <path d="M3.5 6h13M3.5 10h13M3.5 14h13" />
      <rect x="11" y="4.4" width="3.2" height="3.2" rx="0.6" fill="var(--bg)" />
      <rect x="5.6" y="8.4" width="3.2" height="3.2" rx="0.6" fill="var(--bg)" />
      <rect x="9.4" y="12.4" width="3.2" height="3.2" rx="0.6" fill="var(--bg)" />
    </>
  ),
  plus: <path d="M10 4v12M4 10h12" />,
  check: <path d="m4.5 10.4 3.5 3.5 7.5-7.8" />,
  close: <path d="m5 5 10 10M15 5 5 15" />,
  menu: <path d="M3.5 6h13M3.5 10h13M3.5 14h13" />,
  search: (
    <>
      <circle cx="8.8" cy="8.8" r="5" />
      <path d="m12.6 12.6 4 4" />
    </>
  ),
  upload: <path d="M10 13V3.8M6.3 7.3 10 3.6l3.7 3.7M3.5 13.5v2.7h13v-2.7" />,
  copy: (
    <>
      <rect x="7" y="7" width="9.5" height="9.5" rx="1.2" />
      <path d="M13 7V4.7c0-.7-.5-1.2-1.2-1.2H4.7c-.7 0-1.2.5-1.2 1.2v7.1c0 .7.5 1.2 1.2 1.2H7" />
    </>
  ),
  stop: <rect x="5.5" y="5.5" width="9" height="9" rx="1" />,
  play: <path d="M6.5 4.5v11l9-5.5Z" />,
  refresh: <path d="M15.8 8.5A6 6 0 1 0 15 13M16 4v4.6h-4.6" />,
  sun: (
    <>
      <circle cx="10" cy="10" r="3.4" />
      <path d="M10 2.5v1.8M10 15.7v1.8M2.5 10h1.8M15.7 10h1.8M4.7 4.7l1.3 1.3M14 14l1.3 1.3M4.7 15.3 6 14M14 6l1.3-1.3" />
    </>
  ),
  moon: <path d="M15.8 12.4A6.4 6.4 0 0 1 7.6 4.2a6.4 6.4 0 1 0 8.2 8.2Z" />,
  system: (
    <>
      <rect x="3" y="4" width="14" height="10" rx="1.2" />
      <path d="M7.5 16.5h5M10 14v2.5" />
    </>
  ),
  out: <path d="M8 4H4.5v12H8M12 6.5 15.5 10 12 13.5M15.5 10H8" />,
  external: <path d="M11.5 3.5h5v5M16.5 3.5 9.5 10.5M14 11.5v5h-10.5V6H8.5" />,
} as const;

export type IconName = keyof typeof PATHS;

export function Icon({ name, size = 18, label }: { name: IconName; size?: number; label?: string }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 20 20"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      role={label ? "img" : undefined}
      aria-label={label}
      aria-hidden={label ? undefined : true}
      focusable="false"
    >
      {PATHS[name]}
    </svg>
  );
}
