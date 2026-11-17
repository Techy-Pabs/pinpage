import { Link, Navigate, useNavigate, useSearchParams } from "react-router";
import { isSignedIn, safeNext, signIn } from "../api";
import { AuthForm } from "../components/AuthForm";
import { AuthLayout } from "../components/AuthLayout";

export function SignInPage() {
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const next = safeNext(params.get("next"));

  if (isSignedIn()) return <Navigate to={next} replace />;

  return (
    <AuthLayout>
      <AuthForm
        title="Sign in"
        submitLabel="Sign in"
        busyLabel="Signing in…"
        newPassword={false}
        onSubmit={async (email, password) => {
          await signIn(email, password);
          navigate(next, { replace: true });
        }}
        footer={
          <>
            New to Pinpage?{" "}
            <Link to="/register" className="font-medium text-ballpoint underline">
              Create an account
            </Link>
          </>
        }
      />
    </AuthLayout>
  );
}
