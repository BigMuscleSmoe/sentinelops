// Times are shown in UTC everywhere, as the summary lines are built server-side in UTC.

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

export const ms = (iso: string): number => Date.parse(iso);

/** 18:36:24 */
export const clock = (iso: string): string => new Date(ms(iso)).toISOString().slice(11, 19);

/** 18:36 */
export const hhmm = (iso: string): string => clock(iso).slice(0, 5);

/** Nov 26, 18:36 UTC */
export function dated(iso: string): string {
  const d = new Date(ms(iso));
  return `${MONTHS[d.getUTCMonth()]} ${d.getUTCDate()}, ${hhmm(iso)} UTC`;
}

/** +1:38, from one time to another */
export function since(fromIso: string, toIso: string): string {
  const s = Math.max(0, Math.round((ms(toIso) - ms(fromIso)) / 1000));
  return `+${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

export const int = (n: number): string => n.toLocaleString("en-US");

export function usd(n: number): string {
  return n < 0.1 ? `$${n.toFixed(4)}` : `$${n.toFixed(2)}`;
}

export const seconds = (n: number): string => `${Math.round(n)} s`;
