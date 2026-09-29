// Server-side base URL of the FastAPI service. Inside docker-compose this is
// http://api:8000; locally it defaults to the uvicorn dev server.
export const API_URL = process.env.API_URL ?? "http://localhost:8000";

export type Health = {
  status: string;
  version: string;
};

export async function fetchHealth(): Promise<Health | null> {
  try {
    const res = await fetch(`${API_URL}/api/health`, { cache: "no-store" });
    if (!res.ok) return null;
    return (await res.json()) as Health;
  } catch {
    return null;
  }
}
