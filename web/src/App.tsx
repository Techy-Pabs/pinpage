import { useEffect } from "react";
import { Navigate, Route, Routes, useNavigate } from "react-router";
import { HOME, setSessionEndedHandler, signInPath } from "./api";
import { DocumentPage } from "./pages/DocumentPage";
import { LibraryPage } from "./pages/Library";
import { RegisterPage } from "./pages/Register";
import { SignInPage } from "./pages/SignIn";
import { RequireAuth } from "./RequireAuth";

/** When the API says the session ended (401), go to sign in inside the app,
    keeping the page to come back to. */
function SessionEndedRedirect() {
  const navigate = useNavigate();
  useEffect(() => {
    setSessionEndedHandler((next) => navigate(signInPath(next), { replace: true }));
  }, [navigate]);
  return null;
}

export function App() {
  return (
    <>
      <SessionEndedRedirect />
      <Routes>
        <Route path="/sign-in" element={<SignInPage />} />
        <Route path="/register" element={<RegisterPage />} />
        <Route
          path="/library"
          element={
            <RequireAuth>
              <LibraryPage />
            </RequireAuth>
          }
        />
        <Route
          path="/documents/:id"
          element={
            <RequireAuth>
              <DocumentPage />
            </RequireAuth>
          }
        />
        <Route path="*" element={<Navigate to={HOME} replace />} />
      </Routes>
    </>
  );
}
