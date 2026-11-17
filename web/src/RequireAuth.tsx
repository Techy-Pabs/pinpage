import type { ReactNode } from "react";
import { Navigate, useLocation } from "react-router";
import { isSignedIn, signInPath } from "./api";

/** Pages that need a login: a signed-out visitor goes to sign in, and comes
    back here afterwards. */
export function RequireAuth({ children }: { children: ReactNode }) {
  const location = useLocation();
  if (!isSignedIn()) {
    return <Navigate to={signInPath(location.pathname + location.search)} replace />;
  }
  return children;
}
