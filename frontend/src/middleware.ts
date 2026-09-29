// Pages need the single-user session. The cookie is only checked for presence here; the API
// validates it and a 401 on any call sends the user back to /login.
import { NextResponse, type NextRequest } from "next/server";

export function middleware(req: NextRequest) {
  if (req.cookies.has("sg_session")) return NextResponse.next();
  const url = req.nextUrl.clone();
  url.pathname = "/login";
  url.search = `?next=${encodeURIComponent(req.nextUrl.pathname + req.nextUrl.search)}`;
  return NextResponse.redirect(url);
}

export const config = {
  // Everything except the API proxy, the login page and static assets.
  matcher: ["/((?!api/|login|_next/|favicon\\.ico).*)"],
};
