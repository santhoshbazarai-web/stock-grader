// "NSE is blocking automated access" (backend web_session.blocked_message): shown wherever an
// exchange refused every session method, with links to the manual upload routes in Settings.
import Link from "next/link";

export const BLOCKED_MARKER = "is blocking automated access";

export function isBlocked(text: string | null | undefined): boolean {
  return !!text && text.includes(BLOCKED_MARKER);
}

/** What the backend added after the standard sentence, e.g. " (new quarters from yfinance)". */
export function blockedSuffix(text: string): string {
  const i = text.indexOf("instead");
  return i < 0 ? "" : text.slice(i + "instead".length).replace(/^[.;\s]*/, "");
}

export function BlockedNotice({
  site = "NSE",
  compact = false,
  suffix = "",
}: {
  site?: "NSE" | "BSE";
  compact?: boolean;
  suffix?: string;
}) {
  return (
    <span
      role="note"
      className={compact ? "text-xs" : "text-sm"}
      data-testid="blocked-notice"
    >
      {site} is blocking automated access from this connection. Upload{" "}
      <Link href="/settings#results-filings" className="underline">
        XBRL files
      </Link>{" "}
      or a{" "}
      <Link href="/settings#screener-uploads" className="underline">
        Screener export
      </Link>{" "}
      instead.{suffix.trim() ? ` ${suffix.trim()}` : ""}
    </span>
  );
}
