import { Link, Navigate, useNavigate } from "react-router";
import { HOME, isSignedIn, register } from "../api";
import { AuthForm } from "../components/AuthForm";
import { AuthLayout } from "../components/AuthLayout";

export function RegisterPage() {
  const navigate = useNavigate();

  if (isSignedIn()) return <Navigate to={HOME} replace />;

  return (
    <AuthLayout>
      <AuthForm
        title="Create an account"
        submitLabel="Create account"
        busyLabel="Creating your account…"
        newPassword
        onSubmit={async (email, password) => {
          await register(email, password);
          navigate(HOME, { replace: true });
        }}
        footer={
          <>
            Already have an account?{" "}
            <Link to="/sign-in" className="font-medium text-ballpoint underline">
              Sign in
            </Link>
          </>
        }
      />
    </AuthLayout>
  );
}
