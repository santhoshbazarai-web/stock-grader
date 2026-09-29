import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { fetchHealth } from "@/lib/api";

export const dynamic = "force-dynamic";

export default async function Home() {
  const health = await fetchHealth();

  return (
    <main className="mx-auto flex min-h-screen max-w-3xl flex-col gap-6 p-8">
      <h1 className="text-3xl font-semibold tracking-tight">Stock Grader</h1>
      <Card>
        <CardHeader>
          <CardTitle>API status</CardTitle>
          <CardDescription>Backend health check</CardDescription>
        </CardHeader>
        <CardContent className="flex items-center gap-3">
          {health ? (
            <>
              <Badge>{health.status}</Badge>
              <span className="text-muted-foreground text-sm">v{health.version}</span>
            </>
          ) : (
            <Badge variant="destructive">unreachable</Badge>
          )}
        </CardContent>
      </Card>
    </main>
  );
}
