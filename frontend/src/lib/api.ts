// Browser-side API client. Every call goes through the same-origin /api proxy
// (src/app/api/[...path]/route.ts); a 401 sends the user to /login.
import { API_URL } from "@/lib/server-config";

export class ApiError extends Error {
  constructor(
    public status: number,
    public detail: string,
  ) {
    super(detail);
  }
}

function detailOf(body: unknown, fallback: string): string {
  if (body && typeof body === "object" && "detail" in body) {
    const d = (body as { detail: unknown }).detail;
    if (typeof d === "string") return d;
    if (Array.isArray(d)) {
      return d
        .map((e) => (e && typeof e === "object" && "msg" in e ? String(e.msg) : String(e)))
        .join("; ");
    }
  }
  return fallback;
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api${path}`, {
    ...init,
    headers: { ...(init?.body ? { "content-type": "application/json" } : {}), ...init?.headers },
    credentials: "same-origin",
    cache: "no-store",
  });
  if (res.status === 401 && typeof window !== "undefined" && !path.startsWith("/auth/login")) {
    const next = encodeURIComponent(window.location.pathname + window.location.search);
    window.location.assign(`/login?next=${next}`);
    throw new ApiError(401, "not logged in");
  }
  const body: unknown = res.status === 204 ? null : await res.json().catch(() => null);
  if (!res.ok) throw new ApiError(res.status, detailOf(body, res.statusText));
  return body as T;
}

export type Health = {
  status: string;
  version: string;
};

// Server component use (home page): talks to the API directly.
export async function fetchHealth(): Promise<Health | null> {
  try {
    const res = await fetch(`${API_URL}/api/health`, { cache: "no-store" });
    if (!res.ok) return null;
    return (await res.json()) as Health;
  } catch {
    return null;
  }
}
