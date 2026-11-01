/** Formatting for the interface: relative times, durations, counts. */

const RTF = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
const DATE = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" });
const DATETIME = new Intl.DateTimeFormat(undefined, {
  year: "numeric",
  month: "short",
  day: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
});
const TIME = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });

export function ago(value: string | null | undefined, now: number = Date.now()): string {
  if (!value) return "";
  const seconds = (new Date(value).getTime() - now) / 1000;
  const abs = Math.abs(seconds);
  if (abs < 45) return "just now";
  if (abs < 3600) return RTF.format(Math.round(seconds / 60), "minute");
  if (abs < 86_400) return RTF.format(Math.round(seconds / 3600), "hour");
  if (abs < 7 * 86_400) return RTF.format(Math.round(seconds / 86_400), "day");
  return DATE.format(new Date(value));
}

export const dateTime = (value: string | null | undefined) => (value ? DATETIME.format(new Date(value)) : "");
export const time = (value: string | null | undefined) => (value ? TIME.format(new Date(value)) : "");

export function duration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "";
  if (seconds < 1) return `${Math.round(seconds * 1000)} ms`;
  if (seconds < 60) return `${seconds.toFixed(1)} s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes} min ${Math.round(seconds % 60)} s`;
}

export function count(value: number, noun: string, plural = `${noun}s`): string {
  return `${value.toLocaleString()} ${value === 1 ? noun : plural}`;
}

export function tokens(value: number): string {
  return value >= 1000 ? `${(value / 1000).toFixed(1)}k` : String(value);
}

export function bytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

export const shortId = (id: string) => id.slice(0, 8);

export const label = (value: string) => value.replaceAll("_", " ");

/** `<run>/turn/0/call/1` as `turn 0 · call 1`. */
export function callLabel(invocationId: string): string {
  const parts = invocationId.split("/");
  return parts.length === 5 && parts[1] === "turn" ? `turn ${parts[2]} · call ${parts[4]}` : invocationId;
}
